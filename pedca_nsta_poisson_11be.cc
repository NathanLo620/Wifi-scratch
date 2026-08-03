/*
 * P-EDCA Verification: N STA Scenario  (802.11be / EHT)
 *
 * Standard: WIFI_STANDARD_80211be, EhtMcs5 data rate, 5GHz 20MHz (ch36), GI 1600 ns.
 *
 * Use Case:
 * - Scalability test: Simulate N STAs
 * - Compare Delay, Throughput, Retransmission, Packet Loss across Access Categories
 * - Traffic: 0.5 Mbps per STA, VO only
 * - STA1..N: Placed at fixed distance (5m) from AP
 * - All STAs have PedcaSupported=true
 * 
 * Statistics: Using WifiTxStatsHelper for MAC-layer metrics
 */

#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/internet-module.h"
#include "ns3/wifi-module.h"
#include "ns3/mobility-module.h"
#include "ns3/applications-module.h"
#include "ns3/wifi-tx-stats-helper.h"
#include "ns3/qos-frame-exchange-manager.h"
#include "ns3/pedca-controller.h"
#include "ns3/pedca-parameter-set.h"
#include "ns3/wifi-mac-header.h"
#include "ns3/wifi-ppdu.h"
#include "ns3/wifi-psdu.h"
#include "ns3/phy-entity.h"
#include "ns3/seq-ts-header.h"
#include "ns3/udp-socket-factory.h"

#include <iostream>
#include <sstream>
#include <vector>
#include <map>
#include <set>
#include <algorithm>
#include <fstream>

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("PedcaVerificationNSta");

// ── Adaptive P-EDCA: controller trajectory, one row per control step ──
static std::ofstream g_trajCsv;

static void
ControlStepTrace(Time now,
                 uint32_t kHat,
                 uint8_t cwds,
                 uint8_t qsrc,
                 uint8_t psrc,
                 uint32_t nAggressive,
                 uint32_t nConservative,
                 uint32_t nUnchanged,
                 double busyFrac,
                 double collRate,
                 double collRateFrame,
                 uint32_t dsCtsFrames,
                 uint32_t dsCtsBursts,
                 uint32_t bsrSum,
                 uint32_t lliCount,
                 bool changed)
{
    if (!g_trajCsv.is_open())
    {
        return;
    }
    g_trajCsv << now.GetSeconds() << "," << kHat << "," << +cwds << "," << +qsrc << "," << +psrc
              << "," << nAggressive << "," << nConservative << "," << nUnchanged << "," << busyFrac
              << "," << collRate << "," << collRateFrame << "," << dsCtsFrames << ","
              << dsCtsBursts << "," << bsrSum << "," << lliCount << "," << (changed ? 1 : 0)
              << "\n";
}


static double g_apIdleUs = 0;
static double g_warmupTime = 1.0;
static double g_simTime = 10.0;



static std::string FrameTypeStr(const WifiMacHeader& hdr)
{
    if (hdr.IsRts()) return "RTS";
    if (hdr.IsCts()) return "CTS";
    if (hdr.IsAck()) return "ACK";
    if (hdr.IsBlockAck()) return "BACK";
    if (hdr.IsBlockAckReq()) return "BACKREQ";
    if (hdr.IsData()) return "DATA";
    if (hdr.IsMgt()) return "MGT";
    if (hdr.IsCtl()) return "CTL";
    return "OTHER";
}

static std::string FrameInfoFromMpdu(Ptr<const Packet> mpdu)
{
    Packet copy = *mpdu;
    WifiMacHeader hdr;
    copy.RemoveHeader(hdr);
    std::stringstream ss;
    ss << FrameTypeStr(hdr) << " from=" << hdr.GetAddr2() << " to=" << hdr.GetAddr1();
    return ss.str();
}

static std::string FrameInfoFromPpdu(Ptr<const WifiPpdu> ppdu)
{
    auto psdu = ppdu->GetPsdu();
    std::stringstream ss;
    if (psdu && psdu->GetNMpdus() >= 1) {
        const auto& hdr = psdu->GetHeader(0);
        ss << FrameTypeStr(hdr) << " from=" << hdr.GetAddr2() << " to=" << hdr.GetAddr1();
    } else {
        ss << "??";
    }
    return ss.str();
}

static void PhyRxBeginCb(std::string label,
                         Ptr<const Packet> packet,
                         RxPowerWattPerChannelBand /*rxPowers*/)
{
    std::clog << "[PHY-LOCK] " << label
              << " t=" << Simulator::Now().GetMicroSeconds() << "us "
              << FrameInfoFromMpdu(packet) << std::endl;
}

static void PhyRxPpduDropCb(std::string label,
                            Ptr<const WifiPpdu> ppdu,
                            WifiPhyRxfailureReason reason)
{
    std::stringstream rs;
    rs << reason;
    std::clog << "[PHY-DROP] " << label
              << " t=" << Simulator::Now().GetMicroSeconds() << "us "
              << FrameInfoFromPpdu(ppdu)
              << " reason=" << rs.str() << std::endl;
}

static const char* PhyStateName(ns3::WifiPhyState s)
{
    switch (s) {
        case ns3::WifiPhyState::IDLE: return "IDLE";
        case ns3::WifiPhyState::CCA_BUSY: return "CCA_BUSY";
        case ns3::WifiPhyState::TX: return "TX";
        case ns3::WifiPhyState::RX: return "RX";
        case ns3::WifiPhyState::SWITCHING: return "SWITCHING";
        case ns3::WifiPhyState::SLEEP: return "SLEEP";
        case ns3::WifiPhyState::OFF: return "OFF";
        default: return "?";
    }
}

void ApPhyStateTrace(std::string context, Time start, Time duration, ns3::WifiPhyState state)
{
    if (state == ns3::WifiPhyState::IDLE) {
        double startUs = start.GetMicroSeconds();
        double endUs = startUs + duration.GetMicroSeconds();

        double windowStartUs = g_warmupTime * 1000000.0;
        double windowEndUs = g_simTime * 1000000.0;

        // Calculate overlap of [startUs, endUs] with the evaluation window [windowStartUs, windowEndUs]
        double overlapStart = std::max(startUs, windowStartUs);
        double overlapEnd = std::min(endUs, windowEndUs);

        if (overlapEnd > overlapStart) {
            g_apIdleUs += (overlapEnd - overlapStart);
        }
    }

    // Trace AP PHY state to clog so that we can correlate "RTS sent at t=X" with
    // whether the AP was actually able to receive (IDLE/CCA_BUSY/RX/TX) at that instant.
    double startUs = start.GetMicroSeconds();
    double endUs = startUs + duration.GetMicroSeconds();
    std::clog << "[AP-PHY] state=" << PhyStateName(state)
              << " start=" << startUs << "us"
              << " end=" << endUs << "us"
              << " duration=" << duration.GetMicroSeconds() << "us" << std::endl;
}

// Helper to get AC name
static const char* AcName(uint8_t ac)
{
  switch (ac)
  {
    case 0: return "BE";
    case 1: return "BK";
    case 2: return "VI";
    case 3: return "VO";
    default: return "?";
  }
}

// TID to AC mapping (802.11 standard)
static uint8_t TidToAc(uint8_t tid)
{
  switch(tid) {
    case 1: case 2: return 1; // BK
    case 0: case 3: return 0; // BE
    case 4: case 5: return 2; // VI
    case 6: case 7: return 3; // VO
    default: return 0;
  }
}

// ──────────────────────────────────────────────────────────────────────
//  Poisson UDP traffic source
//  ---------------------------------------------------------------------
//  Replaces the CBR stream produced by ns-3's UdpClient with a Poisson
//  arrival process: fixed-size datagrams are emitted at exponentially
//  distributed inter-arrival times. The mean packet rate equals the CBR
//  rate derived from --dataRate, so the *average* offered load is identical
//  to the original simulation — only the arrival-time distribution changes
//  (deterministic period → memoryless Poisson). Packet layout (SeqTsHeader
//  + filler) and IP ToS handling are copied from UdpClient so the resulting
//  MAC frames and access-category mapping are byte-for-byte the same.
// ──────────────────────────────────────────────────────────────────────
class PoissonUdpClient : public Application
{
public:
  static TypeId GetTypeId();
  PoissonUdpClient();
  ~PoissonUdpClient() override = default;

  // meanRate: mean packets per second of the Poisson process.
  void Setup(Address peer, uint32_t pktSize, double meanRate,
             uint8_t tos, uint32_t maxPackets);

private:
  void StartApplication() override;
  void StopApplication() override;
  void Send();

  Ptr<Socket>                    m_socket;
  Address                        m_peer;
  uint32_t                       m_pktSize {0};
  uint8_t                        m_tos {0};
  uint32_t                       m_maxPackets {0};
  uint32_t                       m_sent {0};
  EventId                        m_sendEvent;
  Ptr<ExponentialRandomVariable> m_interval;
};

TypeId
PoissonUdpClient::GetTypeId()
{
  static TypeId tid = TypeId("PoissonUdpClient")
                          .SetParent<Application>()
                          .SetGroupName("Applications")
                          .AddConstructor<PoissonUdpClient>();
  return tid;
}

PoissonUdpClient::PoissonUdpClient()
{
  m_interval = CreateObject<ExponentialRandomVariable>();
}

void
PoissonUdpClient::Setup(Address peer, uint32_t pktSize, double meanRate,
                        uint8_t tos, uint32_t maxPackets)
{
  m_peer = peer;
  m_pktSize = pktSize;
  m_tos = tos;
  m_maxPackets = maxPackets;
  // ExponentialRandomVariable "Mean" is the mean inter-arrival time (s).
  // Mean = 1/rate reproduces a Poisson process with the requested mean rate.
  m_interval->SetAttribute("Mean", DoubleValue(1.0 / meanRate));
}

void
PoissonUdpClient::StartApplication()
{
  if (!m_socket)
  {
    m_socket = Socket::CreateSocket(GetNode(), UdpSocketFactory::GetTypeId());
    m_socket->Bind();
    m_socket->SetIpTos(m_tos); // Affects only IPv4 sockets (same as UdpClient).
    m_socket->Connect(m_peer);
  }
  // First arrival one exponential inter-arrival time after the app starts.
  m_sendEvent = Simulator::Schedule(Seconds(m_interval->GetValue()),
                                    &PoissonUdpClient::Send, this);
}

void
PoissonUdpClient::StopApplication()
{
  if (m_sendEvent.IsPending())
  {
    Simulator::Cancel(m_sendEvent);
  }
  if (m_socket)
  {
    m_socket->Close();
  }
}

void
PoissonUdpClient::Send()
{
  SeqTsHeader seqTs;
  seqTs.SetSeq(m_sent);
  NS_ABORT_IF(m_pktSize < seqTs.GetSerializedSize());
  Ptr<Packet> p = Create<Packet>(m_pktSize - seqTs.GetSerializedSize());
  p->AddHeader(seqTs);
  m_socket->Send(p);
  ++m_sent;

  if (m_sent < m_maxPackets)
  {
    m_sendEvent = Simulator::Schedule(Seconds(m_interval->GetValue()),
                                      &PoissonUdpClient::Send, this);
  }
}

// ---------------------- Main ----------------------

int main(int argc, char* argv[])
{
  uint32_t nSta = 20;
  double simTime = 3.0;
  std::string dataRate = "1Mbps";
  uint32_t payloadSize = 1000;
  bool enableRts = true;
  bool enableAggregation = true;
  uint32_t baBufferSize = 64;    // Block Ack window size, in MPDUs (match HT)
  uint32_t maxAmpduSize = 65535; // Maximum A-MPDU size, in bytes
  uint32_t maxAmsduSize = 7935;  // Maximum A-MSDU size, in bytes
  bool verbose = false;
  double warmupTime = 1.0;
  uint32_t voicePdfBinUs = 5;
  std::string voicePdfOutput = "scratch/delay_pdf/pedca_vo_delay_pdf.csv";
  std::string pedcaStaDelayOutput = "";  // CSV for P-EDCA STA delay histogram
  std::string legacyStaDelayOutput = ""; // CSV for Legacy STA delay histogram
  double   pedcaRatio = 0.05; // Fraction of STAs with P-EDCA enabled (0.0-1.0)
  uint32_t cwds = 0;          // P-EDCA Stage-1 CW (0=ASAP, 1=random[0,1], ...)
  uint32_t qsrc = 2;          // dot11PEDCARetryThreshold   (QSRC trigger threshold)
  uint32_t psrc = 1;          // dot11PEDCAConsecutiveAttempt (max consecutive P-EDCA attempts)
  std::string clogFile = "scratch/pedca_stage2_stats.log";

  // ── Adaptive P-EDCA closed loop ──
  bool     adaptive = false;        // opt-in: keeps existing sweeps bit-identical by default
  std::string policy = "kdriven";   // kdriven | v2 | fixed
  int32_t  kOverride = -1;          // force the P-EDCA STA count instead of estimating it
  double   qsrcIntercept = 0.5;     // QSRC = clamp(round(intercept + slope*k), 0, 5)
  double   qsrcSlope = 0.2;
  uint32_t cwdsKDriven = 1;
  uint32_t psrc3MaxK = 10;
  uint32_t psrc2MaxK = 22;
  uint32_t controlPeriodMs = 100;
  double   delayBoundMs = 10.0;
  double   lliFraction = 0.7;
  double   busyHigh = 0.85;
  double   collHigh = 0.90;
  double   collLow = 0.30;
  uint32_t lliHigh = 5;
  double   bsrHigh = 8.0;
  uint32_t cwdsMax = 2;
  uint32_t qsrcAggressive = 1;
  uint32_t psrcAggressive = 3;
  uint32_t qsrcConservative = 5;
  uint32_t psrcConservative = 1;
  bool     burstCollRate = true;
  double   ctrlStart = -1.0;        // <0 means warmupTime
  std::string trajOutput = "";      // omit the flag entirely to disable


  CommandLine cmd(__FILE__);
  cmd.AddValue("nSta",   "Number of stations", nSta);
  cmd.AddValue("simTime","Simulation time (seconds)", simTime);
  cmd.AddValue("dataRate","Data rate (e.g., 0.5Mbps)", dataRate);
  cmd.AddValue("verbose","Enable logging", verbose);
  cmd.AddValue("enableAggregation","Enable A-MPDU/A-MSDU aggregation for all ACs", enableAggregation);
  cmd.AddValue("baBufferSize","Block Ack buffer/window size in MPDUs", baBufferSize);
  cmd.AddValue("maxAmpduSize","Maximum A-MPDU size in bytes when aggregation is enabled", maxAmpduSize);
  cmd.AddValue("maxAmsduSize","Maximum A-MSDU size in bytes when aggregation is enabled", maxAmsduSize);
  cmd.AddValue("voicePdfBinUs","VO delay PDF bin width (microseconds)", voicePdfBinUs);
  cmd.AddValue("voicePdfOutput","Output CSV file for VO delay PDF", voicePdfOutput);
  cmd.AddValue("pedcaStaDelayOutput","CSV for P-EDCA STA delay PDF", pedcaStaDelayOutput);
  cmd.AddValue("legacyStaDelayOutput","CSV for Legacy STA delay PDF", legacyStaDelayOutput);
  cmd.AddValue("pedcaRatio","Fraction of STAs with P-EDCA enabled (0.0-1.0)", pedcaRatio);
  cmd.AddValue("cwds","P-EDCA Stage-1 CW (0=ASAP, 1=random[0,1])", cwds);
  cmd.AddValue("qsrc","dot11PEDCARetryThreshold: QSRC must reach this to trigger P-EDCA", qsrc);
  cmd.AddValue("psrc","dot11PEDCAConsecutiveAttempt: max consecutive P-EDCA attempts", psrc);
  cmd.AddValue("clogFile","Redirect std::clog to this file (empty = stderr)", clogFile);
  cmd.AddValue("adaptive","Run the AP-side P-EDCA controller (0 = plain static P-EDCA)", adaptive);
  cmd.AddValue("policy","Controller rule set: kdriven | v2 | fixed", policy);
  cmd.AddValue("kOverride","Force the P-EDCA STA count the controller assumes; <0 = estimate", kOverride);
  cmd.AddValue("qsrcIntercept","Intercept of the k-driven QSRC law", qsrcIntercept);
  cmd.AddValue("qsrcSlope","Slope per P-EDCA STA of the k-driven QSRC law", qsrcSlope);
  cmd.AddValue("cwdsKDriven","CWds advertised by the k-driven policy", cwdsKDriven);
  cmd.AddValue("psrc3MaxK","Largest k that still receives PSRC 3", psrc3MaxK);
  cmd.AddValue("psrc2MaxK","Largest k that still receives PSRC 2", psrc2MaxK);
  cmd.AddValue("controlPeriodMs","Controller period in milliseconds", controlPeriodMs);
  cmd.AddValue("ctrlStart","When the controller starts observing (s); <0 means warmupTime", ctrlStart);
  cmd.AddValue("delayBoundMs","AC_VO delay budget the LLI trigger measures against (ms)", delayBoundMs);
  cmd.AddValue("lliFraction","Fraction of the delay bound beyond which LLI is set", lliFraction);
  cmd.AddValue("busyHigh","V2: busy fraction turning on the conservative gate", busyHigh);
  cmd.AddValue("collHigh","V2: DS-CTS collision rate pushing CWds to cwdsMax", collHigh);
  cmd.AddValue("collLow","V2: DS-CTS collision rate dropping CWds to 0", collLow);
  cmd.AddValue("lliHigh","V2: per-STA LLI count marking delay pressure", lliHigh);
  cmd.AddValue("bsrHigh","V2: per-STA buffer status marking queue pressure", bsrHigh);
  cmd.AddValue("cwdsMax","V2: largest CWds the controller may choose", cwdsMax);
  cmd.AddValue("qsrcAggressive","V2: QSRC of the aggressive corner", qsrcAggressive);
  cmd.AddValue("psrcAggressive","V2: PSRC of the aggressive corner", psrcAggressive);
  cmd.AddValue("qsrcConservative","V2: QSRC of the conservative corner", qsrcConservative);
  cmd.AddValue("psrcConservative","V2: PSRC of the conservative corner", psrcConservative);
  cmd.AddValue("burstCollRate","V2: measure DS-CTS collisions per burst (1) or per frame (0)", burstCollRate);
  cmd.AddValue("trajOutput","Controller trajectory CSV; omit the flag entirely to disable", trajOutput);
  cmd.Parse(argc, argv);

  if (ctrlStart < 0.0)
  {
      ctrlStart = warmupTime;
  }

  // ── Redirect std::clog (where all the [P-EDCA ...] / [RTS-RX] / [RTS SENT] traces go)
  //    to a dedicated log file so the user can post-process it.
  std::ofstream clogStream;
  std::streambuf* originalClogBuf = nullptr;
  if (!clogFile.empty()) {
    clogStream.open(clogFile, std::ios::out | std::ios::trunc);
    if (clogStream.is_open()) {
      originalClogBuf = std::clog.rdbuf();
      std::clog.rdbuf(clogStream.rdbuf());
      std::cout << "[INFO] std::clog redirected to: " << clogFile << "\n";
    } else {
      std::cout << "[WARN] Failed to open clog file: " << clogFile
                << " — keeping default stderr.\n";
    }
  }
  
  if (verbose) {
    LogComponentEnable("PedcaVerificationNSta", LOG_LEVEL_INFO);
  }
  
  g_warmupTime = warmupTime;
  g_simTime = simTime;
  

  
  NodeContainer wifiStaNodes;
  wifiStaNodes.Create(nSta);
  NodeContainer wifiApNode;
  wifiApNode.Create(1);

  YansWifiChannelHelper channel = YansWifiChannelHelper::Default();
  YansWifiPhyHelper phy;
  phy.SetChannel(channel.Create());
  phy.Set("ChannelSettings", StringValue("{36, 20, BAND_5GHZ, 0}")); // 5GHz

  WifiHelper wifi;
  wifi.SetStandard(WIFI_STANDARD_80211be);
  wifi.ConfigHeOptions("GuardInterval", TimeValue(NanoSeconds(800)));
  wifi.SetRemoteStationManager("ns3::ConstantRateWifiManager",
                               "DataMode", StringValue("EhtMcs6"),
                               "ControlMode", StringValue("OfdmRate6Mbps"));
  
  // RTS/CTS
  if (!enableRts)
    Config::SetDefault("ns3::WifiRemoteStationManager::RtsCtsThreshold", StringValue("999999"));
  else
    Config::SetDefault("ns3::WifiRemoteStationManager::RtsCtsThreshold", StringValue("0"));
  
  // Queue size: 400 packets
  Config::SetDefault("ns3::WifiMacQueue::MaxSize", StringValue("10000p"));

  // Aggregation control. Disabled by default to preserve legacy verification behavior.
  Config::SetDefault("ns3::WifiMac::MpduBufferSize", UintegerValue(baBufferSize));
  const uint32_t effectiveMaxAmpduSize = enableAggregation ? maxAmpduSize : 0;
  const uint32_t effectiveMaxAmsduSize = enableAggregation ? maxAmsduSize : 0;
  Config::SetDefault("ns3::WifiMac::VO_MaxAmpduSize", UintegerValue(effectiveMaxAmpduSize));
  Config::SetDefault("ns3::WifiMac::VI_MaxAmpduSize", UintegerValue(effectiveMaxAmpduSize));
  Config::SetDefault("ns3::WifiMac::BE_MaxAmpduSize", UintegerValue(effectiveMaxAmpduSize));
  Config::SetDefault("ns3::WifiMac::BK_MaxAmpduSize", UintegerValue(effectiveMaxAmpduSize));
  Config::SetDefault("ns3::WifiMac::VO_MaxAmsduSize", UintegerValue(effectiveMaxAmsduSize));
  Config::SetDefault("ns3::WifiMac::VI_MaxAmsduSize", UintegerValue(effectiveMaxAmsduSize));
  Config::SetDefault("ns3::WifiMac::BE_MaxAmsduSize", UintegerValue(effectiveMaxAmsduSize));
  Config::SetDefault("ns3::WifiMac::BK_MaxAmsduSize", UintegerValue(effectiveMaxAmsduSize));
  
  // ── Adaptive P-EDCA feedback path. Confined to the adaptive branch so that --adaptive=0
  //    leaves this scenario bit-identical to what it was before the controller existed.
  if (adaptive)
  {
      Config::SetDefault("ns3::QosFrameExchangeManager::SetQueueSize", BooleanValue(true));
      // The 20 ms default expires a buffer status report before a 100 ms control step reads it.
      Config::SetDefault("ns3::ApWifiMac::BsrLifetime",
                         TimeValue(MilliSeconds(2 * controlPeriodMs)));
      Config::SetDefault("ns3::QosFrameExchangeManager::PedcaLliDelayBound",
                         TimeValue(MilliSeconds(delayBoundMs)));
      Config::SetDefault("ns3::QosFrameExchangeManager::PedcaLliFraction",
                         DoubleValue(lliFraction));
  }

  Ssid ssid = Ssid("wifi-backoff-vo");

  // AP Setup
  WifiMacHelper mac;
  mac.SetType("ns3::ApWifiMac",
              "Ssid", SsidValue(ssid),
              "QosSupported", BooleanValue(true),
              "PedcaControl", BooleanValue(adaptive));
  NetDeviceContainer apDevices = wifi.Install(phy, mac, wifiApNode);
  
  // STA Setup - P-EDCA enabled for first nPedcaSta STAs
  uint32_t nPedcaSta = static_cast<uint32_t>(nSta * pedcaRatio);
  NetDeviceContainer staDevices;
  for (uint32_t i = 0; i < nSta; ++i)
  {
      bool pedcaEnabled = (i < nPedcaSta);
      mac.SetType("ns3::StaWifiMac",
                  "Ssid", SsidValue(ssid),
                  "QosSupported", BooleanValue(true),
                  "PedcaSupported", BooleanValue(pedcaEnabled),
                  "ActiveProbing", BooleanValue(false));
                  
      staDevices.Add(wifi.Install(phy, mac, wifiStaNodes.Get(i)));
  }

  // Apply CWds / QSRC-threshold / PSRC-limit to every P-EDCA STA's FEM.
  // All three are now runtime parameters — no rebuild needed when sweeping.
  for (uint32_t i = 0; i < nPedcaSta; ++i)
  {
      auto wDev = DynamicCast<WifiNetDevice>(staDevices.Get(i));
      auto fem  = wDev->GetMac()->GetFrameExchangeManager(0);
      auto qFem = DynamicCast<QosFrameExchangeManager>(fem);
      if (qFem) {
          qFem->SetCwds(cwds);
          qFem->SetQsrc(static_cast<uint16_t>(qsrc));
          qFem->SetPsrc(static_cast<uint8_t>(psrc));
      }
  }

  // ── AP-side controller. Both arms seed the STAs with the same theta above, so the adaptive
  //    arm starts exactly where the fixed arm stays.
  Ptr<PedcaController> pedcaController;
  if (adaptive)
  {
      pedcaController = CreateObject<PedcaController>();
      pedcaController->SetAttribute("Policy", StringValue(policy));
      pedcaController->SetAttribute("KOverride", IntegerValue(kOverride));
      pedcaController->SetAttribute("QsrcIntercept", DoubleValue(qsrcIntercept));
      pedcaController->SetAttribute("QsrcSlope", DoubleValue(qsrcSlope));
      pedcaController->SetAttribute("CwdsKDriven", UintegerValue(cwdsKDriven));
      pedcaController->SetAttribute("Psrc3MaxK", UintegerValue(psrc3MaxK));
      pedcaController->SetAttribute("Psrc2MaxK", UintegerValue(psrc2MaxK));
      pedcaController->SetAttribute("Period", TimeValue(MilliSeconds(controlPeriodMs)));
      pedcaController->SetAttribute("BusyHigh", DoubleValue(busyHigh));
      pedcaController->SetAttribute("CollHigh", DoubleValue(collHigh));
      pedcaController->SetAttribute("CollLow", DoubleValue(collLow));
      pedcaController->SetAttribute("LliHigh", UintegerValue(lliHigh));
      pedcaController->SetAttribute("BsrPerStaHigh", DoubleValue(bsrHigh));
      pedcaController->SetAttribute("CwdsMax", UintegerValue(cwdsMax));
      pedcaController->SetAttribute("QsrcAggressive", UintegerValue(qsrcAggressive));
      pedcaController->SetAttribute("PsrcAggressive", UintegerValue(psrcAggressive));
      pedcaController->SetAttribute("QsrcConservative", UintegerValue(qsrcConservative));
      pedcaController->SetAttribute("PsrcConservative", UintegerValue(psrcConservative));
      pedcaController->SetAttribute("UseBurstCollRate", BooleanValue(burstCollRate));
      pedcaController->Setup(DynamicCast<WifiNetDevice>(apDevices.Get(0)));
      pedcaController->SetInitialTheta(PedcaTheta{static_cast<uint8_t>(cwds),
                                                 static_cast<uint8_t>(qsrc),
                                                 static_cast<uint8_t>(psrc)});

      if (!trajOutput.empty())
      {
          g_trajCsv.open(trajOutput, std::ios::out | std::ios::trunc);
          if (g_trajCsv.is_open())
          {
              g_trajCsv << "time_s,k_hat,cwds,qsrc,psrc,n_aggressive,n_conservative,"
                           "n_unchanged,busy_frac,coll_rate,coll_rate_frame,dscts_frames,"
                           "dscts_bursts,bsr_sum,lli_count,changed\n";
              pedcaController->TraceConnectWithoutContext("ControlStep",
                                                          MakeCallback(&ControlStepTrace));
          }
      }

      pedcaController->Start(Seconds(ctrlStart));
      std::clog << "[P-EDCA CTRL] adaptive on: policy=" << policy
                << " period=" << controlPeriodMs << "ms ctrlStart=" << ctrlStart
                << "s trueNPedca=" << nPedcaSta << std::endl;
  }

  // ---------------------- WifiTxStatsHelper ----------------------
  WifiTxStatsHelper wifiTxStats;
  wifiTxStats.Enable(apDevices);
  wifiTxStats.Enable(staDevices);
  wifiTxStats.Start(Seconds(warmupTime));
  wifiTxStats.Stop(Seconds(simTime));

  // Mobility
  MobilityHelper mobility;
  mobility.SetMobilityModel("ns3::ConstantPositionMobilityModel");
  
  Ptr<ListPositionAllocator> apPos = CreateObject<ListPositionAllocator>();
  apPos->Add(Vector(0.0, 0.0, 0.0));
  mobility.SetPositionAllocator(apPos);
  mobility.Install(wifiApNode);
  
  // Place STAs randomly in a disc of radius 1~5m (same as wifi_backoff80211n.cc)
  mobility.SetPositionAllocator("ns3::RandomDiscPositionAllocator",
                                "X", StringValue("0.0"),
                                "Y", StringValue("0.0"),
                                "Rho", StringValue("ns3::UniformRandomVariable[Min=1.0|Max=5.0]"));
  mobility.Install(wifiStaNodes);

  // Internet
  InternetStackHelper stack;
  stack.Install(wifiApNode);
  stack.Install(wifiStaNodes);

  Ipv4AddressHelper address;
  address.SetBase("10.1.1.0", "255.255.255.0");
  Ipv4InterfaceContainer apIf = address.Assign(apDevices);
  Ipv4InterfaceContainer staIf = address.Assign(staDevices);

  // Traffic: UDP Server on AP (VO only)
  uint16_t basePort = 5000;
  constexpr uint8_t voAc = 3;
  constexpr uint8_t voTos = 0xC0;
  UdpServerHelper server(basePort + voAc);
  ApplicationContainer serverApp = server.Install(wifiApNode.Get(0));
  serverApp.Start(Seconds(0.5));
  serverApp.Stop(Seconds(simTime));
  
  // Clients on STAs: Each STA sends VO traffic only.
  // Traffic model: Poisson arrivals. The mean packet rate matches the CBR
  // rate derived from --dataRate (packetsPerSecond below), so the average
  // offered load is unchanged; inter-arrival times are now exponential.
  DataRate rate(dataRate);
  double packetsPerSecond = rate.GetBitRate() / (8.0 * payloadSize);

  Ptr<UniformRandomVariable> startRv = CreateObject<UniformRandomVariable>();

  InetSocketAddress dest(apIf.GetAddress(0), basePort + voAc);

  for (uint32_t i = 0; i < nSta; ++i)
  {
      Ptr<PoissonUdpClient> client = CreateObject<PoissonUdpClient>();
      client->Setup(dest, payloadSize, packetsPerSecond, voTos, 100000);
      wifiStaNodes.Get(i)->AddApplication(client);

      double start = 0.5 + startRv->GetValue(0.0, 0.5);
      client->SetStartTime(Seconds(start));
      client->SetStopTime(Seconds(simTime));
  }

  std::string apPhyStatePath = "/NodeList/" + std::to_string(wifiApNode.Get(0)->GetId()) + "/DeviceList/*/$ns3::WifiNetDevice/Phy/State/State";
  Config::Connect(apPhyStatePath, MakeCallback(&ApPhyStateTrace));

  // ── PHY-level RX traces on every node: lets us tell, for each receiver,
  //    which frame its PHY locked onto (PHY-LOCK) vs which frames it dropped
  //    (PHY-DROP) when multiple transmissions overlap.
  auto connectPhyTraces = [](Ptr<NetDevice> dev, const std::string& label) {
    Ptr<WifiNetDevice> wdev = DynamicCast<WifiNetDevice>(dev);
    if (!wdev) return;
    Ptr<WifiPhy> phy = wdev->GetPhy();
    if (!phy) return;
    phy->TraceConnectWithoutContext(
        "PhyRxBegin",
        MakeBoundCallback(&PhyRxBeginCb, label));
    phy->TraceConnectWithoutContext(
        "PhyRxPpduDrop",
        MakeBoundCallback(&PhyRxPpduDropCb, label));
  };
  connectPhyTraces(apDevices.Get(0), "AP");
  for (uint32_t i = 0; i < nSta; ++i) {
    std::stringstream l;
    l << "STA" << i << ((i < nPedcaSta) ? "(P)" : "(L)");
    connectPhyTraces(staDevices.Get(i), l.str());
  }

  // ── Print STA/AP address mapping into the log so the user can identify the
  //    P-EDCA STA in [RTS SENT] / [RTS-RX] traces.
  {
    std::clog << "================ P-EDCA Stage 2 Diagnostic Log ================\n";
    std::clog << "nSta=" << nSta << "  P-EDCA STAs=" << nPedcaSta
              << "  simTime=" << simTime << "s  warmup=" << warmupTime << "s\n";
    Ptr<WifiNetDevice> apW = DynamicCast<WifiNetDevice>(apDevices.Get(0));
    std::clog << "AP MAC address: " << apW->GetMac()->GetAddress() << "\n";
    for (uint32_t i = 0; i < nSta; ++i) {
      Ptr<WifiNetDevice> w = DynamicCast<WifiNetDevice>(staDevices.Get(i));
      std::clog << "STA" << i << " MAC=" << w->GetMac()->GetAddress()
                << "  type=" << ((i < nPedcaSta) ? "P-EDCA" : "Legacy") << "\n";
    }
    std::clog << "================================================================\n";
  }

  Simulator::Stop(Seconds(simTime + 1.0));
  Simulator::Run();
  
  // ---------------------- WifiTxStatsHelper Output ----------------------
  double duration = simTime - warmupTime;
  if (duration <= 0) duration = 1.0;

// ── P-EDCA Detailed Trace ──
  // Aggregate counters from all P-EDCA STAs' QosFrameExchangeManagers
  uint32_t totalDsCtsSent = 0, totalStage2Entry = 0, totalStage2TxStart = 0;
  uint32_t totalPedcaSuccess = 0, totalEdcaVoSuccess = 0;
  uint32_t totalFailRtsCtsTimeout = 0, totalFailRtsCollision = 0;
  uint32_t totalFailTimingExpired = 0, totalFailDeferral = 0;

  double totalRatio = 0.0;
  double totalSuccessRate = 0.0;
  uint32_t staWithAttempts = 0;

  std::cout << "\n--- Per-STA P-EDCA Detail ---\n";
  for (uint32_t i = 0; i < nSta; ++i)
  {
      Ptr<NetDevice> dev = staDevices.Get(i);
      Ptr<WifiNetDevice> wifiDev = DynamicCast<WifiNetDevice>(dev);
      if (!wifiDev) continue;
      Ptr<WifiMac> mac = wifiDev->GetMac();
      if (!mac) continue;
      // Get the FEM for link 0
      auto fem = mac->GetFrameExchangeManager(0);
      auto qosFem = DynamicCast<QosFrameExchangeManager>(fem);
      if (!qosFem) continue;

      uint32_t dscts = qosFem->GetDsCtsCount();
      uint32_t s2entry = qosFem->GetStage2EntryCount();
      uint32_t s2tx = qosFem->GetStage2TxStartCount();
      uint32_t pedcaSucc = qosFem->GetPedcaSuccessCount();
      uint32_t edcaVoSucc = qosFem->GetEdcaVoSuccessCount();
      uint32_t failCts = qosFem->GetPedcaFailRtsCtsTimeout();
      uint32_t failColl = qosFem->GetPedcaFailRtsCollision();
      uint32_t failExp = qosFem->GetPedcaFailTimingExpired();
      uint32_t failDef = qosFem->GetPedcaFailDeferral();

      totalDsCtsSent += dscts;
      totalStage2Entry += s2entry;
      totalStage2TxStart += s2tx;
      totalPedcaSuccess += pedcaSucc;
      totalEdcaVoSuccess += edcaVoSucc;
      totalFailRtsCtsTimeout += failCts;
      totalFailRtsCollision += failColl;
      totalFailTimingExpired += failExp;
      totalFailDeferral += failDef;

      if (i < nPedcaSta) {
          uint32_t totalVoTx = pedcaSucc + edcaVoSucc;
          double ratio = 0.0;
          if (totalVoTx > 0) {
              ratio = (double)pedcaSucc / (double)totalVoTx;
          }
          totalRatio += ratio;
          
          if (dscts > 0) { // Using DS-CTS sent as attempts
              totalSuccessRate += (double)pedcaSucc / (double)dscts;
              staWithAttempts++;
          }
      }

      // Per-STA detail (only for P-EDCA STAs that had activity)
      if (i < nPedcaSta && (dscts > 0 || pedcaSucc > 0 || edcaVoSucc > 0)) {
          std::cout << "  STA" << i << ": DS-CTS=" << dscts
                    << " S2Entry=" << s2entry << " S2Tx=" << s2tx
                    << " PedcaOK=" << pedcaSucc << " EdcaOK=" << edcaVoSucc
                    << " FailCTS=" << failCts << " FailColl=" << failColl
                    << " FailExp=" << failExp << " Defer=" << failDef << "\n";
      }
  }

  double avgPedcaTxRatio = (nPedcaSta > 0) ? (totalRatio / nPedcaSta) : 0.0;
  double avgPedcaSuccessRate = (staWithAttempts > 0) ? (totalSuccessRate / staWithAttempts) : 0.0;
  
  std::cout << "\n=== General Statistics ===\n";
  std::cout << "P-EDCA Ratio: " << pedcaRatio << "\n";
  std::cout << "P-EDCA STAs: " << nPedcaSta << "/" << nSta << "\n";
  std::cout << "P-EDCA Share (Avg Per-STA P-EDCA Tx/Total Tx): " << (avgPedcaTxRatio * 100.0) << " %\n";
  std::cout << "Avg P-EDCA Attempt Success Rate: " << (avgPedcaSuccessRate * 100.0) << " %\n";
  std::cout << "Global P-EDCA Tx Success: " << totalPedcaSuccess << "\n";
  std::cout << "Global EDCA Tx Success: " << totalEdcaVoSuccess << "\n";
  std::cout << "Global P-EDCA Attempt (DS-CTS Sent): " << totalDsCtsSent << "\n";

  std::cout << "\n--- P-EDCA Detailed Trace ---\n";
  std::cout << "Stage 2 Entered: " << totalStage2Entry << "\n";
  std::cout << "Stage 2 TX Started: " << totalStage2TxStart << "\n";
  std::cout << "P-EDCA Fail RTS No CTS: " << totalFailRtsCtsTimeout << "\n";
  std::cout << "P-EDCA Fail RTS Collision: " << totalFailRtsCollision << "\n";
  std::cout << "P-EDCA Fail Timing Expired: " << totalFailTimingExpired << "\n";
  std::cout << "P-EDCA Fail Deferral: " << totalFailDeferral << "\n";

  uint32_t totalVoTx = totalPedcaSuccess + totalEdcaVoSuccess;
  double globalPedcaShare = (totalVoTx > 0) ? ((double)totalPedcaSuccess / totalVoTx * 100.0) : 0.0;
  double globalEdcaShare = (totalVoTx > 0) ? ((double)totalEdcaVoSuccess / totalVoTx * 100.0) : 0.0;
  std::cout << "Total VO TX (P-EDCA+EDCA): " << totalVoTx << "\n";
  std::cout << "Global P-EDCA Share: " << globalPedcaShare << " %\n";
  std::cout << "Global EDCA Share: " << globalEdcaShare << " %\n";
  
  double totalSimUs = (simTime - warmupTime) * 1000000.0;
  double idleRatio = (totalSimUs > 0) ? (g_apIdleUs / totalSimUs * 100.0) : 0.0;
  std::cout << "Channel Idle Time (AP): " << idleRatio << " % (" << g_apIdleUs << " us / " << totalSimUs << " us)\n";
  
  std::cout << "Total Successes:       " << wifiTxStats.GetSuccesses() << "\n";
  std::cout << "Total Failures:        " << wifiTxStats.GetFailures() << "\n";
  std::cout << "Total Retransmissions: " << wifiTxStats.GetRetransmissions() << "\n\n";

  // ── Build node-ID sets for P-EDCA vs Legacy STAs ──
  std::set<uint32_t> pedcaNodeIds, legacyNodeIds;
  for (uint32_t i = 0; i < nSta; ++i)
  {
      uint32_t nid = wifiStaNodes.Get(i)->GetId();
      if (i < nPedcaSta)
          pedcaNodeIds.insert(nid);
      else
          legacyNodeIds.insert(nid);
  }

  // 1. Calculate Failure Statistics FIRST (needed for Loss calculation)
  auto failureRecs = wifiTxStats.GetFailureRecords();
  std::vector<uint64_t> helperFail(4, 0);
  std::map<std::pair<uint8_t, WifiMacDropReason>, uint64_t> failByReason;
  // [Stat 1] Partition failures by STA type
  uint64_t pedcaStaFailCount = 0, legacyStaFailCount = 0;

  for (const auto& [key, records] : failureRecs) {
    for (const auto& rec : records) {
      uint8_t ac = (rec.m_tid < 8) ? TidToAc(rec.m_tid) : 0;
      helperFail[ac]++;
      if (rec.m_dropReason.has_value()) {
        failByReason[{ac, rec.m_dropReason.value()}]++;
      }
      if (pedcaNodeIds.count(rec.m_nodeId)) pedcaStaFailCount++;
      else if (legacyNodeIds.count(rec.m_nodeId)) legacyStaFailCount++;
    }
  }

  // 2. Calculate Success Statistics and Print All
  auto successRecs = wifiTxStats.GetSuccessRecords();
  std::vector<uint64_t> helperSucc(4, 0), helperRetxs(4, 0), helperCount(4, 0);
  std::vector<double> helperQueueDelay(4, 0.0), helperAccessDelay(4, 0.0), helperMacDelay(4, 0.0);
  std::vector<double> voiceMacDelayUs;

  // [Stat 1] Delay partitioned by P-EDCA STAs vs Legacy STAs
  uint64_t pedcaStaSuccCount = 0, legacyStaSuccCount = 0;
  double pedcaStaQueueDelay = 0.0, pedcaStaAccessDelay = 0.0, pedcaStaMacDelay = 0.0;
  double legacyStaQueueDelay = 0.0, legacyStaAccessDelay = 0.0, legacyStaMacDelay = 0.0;
  std::vector<double> pedcaStaMacDelayVec, legacyStaMacDelayVec;
  // [Stat 4] Legacy EDCA one-shot success (zero retransmissions)
  uint64_t legacyStaZeroRetx = 0;
  // [Stat 4 extended] P-EDCA STA one-shot success (zero retransmissions at MAC level)
  uint64_t pedcaStaZeroRetx = 0;

  for (const auto& [key, records] : successRecs) {
    for (const auto& rec : records) {
      uint8_t ac = (rec.m_tid < 8) ? TidToAc(rec.m_tid) : 0;
      helperSucc[ac]++;
      helperRetxs[ac] += rec.m_retransmissions;
      double queueUs = (rec.m_txStartTime - rec.m_enqueueTime).GetMicroSeconds();
      double accessUs = (rec.m_ackTime - rec.m_txStartTime).GetMicroSeconds();
      helperQueueDelay[ac] += queueUs;
      helperAccessDelay[ac] += accessUs;
      helperMacDelay[ac] += queueUs + accessUs;
      helperCount[ac]++;
      if (ac == voAc) {
        voiceMacDelayUs.push_back(queueUs + accessUs);
      }

      // Partition by STA type
      double macUs = queueUs + accessUs;
      if (pedcaNodeIds.count(rec.m_nodeId)) {
        pedcaStaSuccCount++;
        pedcaStaQueueDelay += queueUs;
        pedcaStaAccessDelay += accessUs;
        pedcaStaMacDelay += macUs;
        pedcaStaMacDelayVec.push_back(macUs);
        if (rec.m_retransmissions == 0) pedcaStaZeroRetx++;
      } else if (legacyNodeIds.count(rec.m_nodeId)) {
        legacyStaSuccCount++;
        legacyStaQueueDelay += queueUs;
        legacyStaAccessDelay += accessUs;
        legacyStaMacDelay += macUs;
        legacyStaMacDelayVec.push_back(macUs);
        if (rec.m_retransmissions == 0) legacyStaZeroRetx++;
      }
    }
  }

  std::cout << "--- Per-AC Success Statistics ---\n";
  for (int ac = 0; ac < 4; ++ac) {
    // Safety check for avg calculations
    double avgQueue = (helperCount[ac]>0) ? (helperQueueDelay[ac] / helperCount[ac]) : 0.0;
    double avgAccess = (helperCount[ac]>0) ? (helperAccessDelay[ac] / helperCount[ac]) : 0.0;
    double avgMac = (helperCount[ac]>0) ? (helperMacDelay[ac] / helperCount[ac]) : 0.0;
    double avgRetx = (helperCount[ac]>0) ? ((double)helperRetxs[ac] / helperCount[ac]) : 0.0;

    // Helper-based Throughput and Loss
    double hThr = (helperSucc[ac] * payloadSize * 8.0) / duration / 1e6;
    double hLoss = 0.0;
    if ((helperSucc[ac] + helperFail[ac]) > 0) {
      hLoss = (double)helperFail[ac] / (double)(helperSucc[ac] + helperFail[ac]) * 100.0;
    }

    // Only print if there is ANY activity (Success OR Failure)
    if (helperSucc[ac] == 0 && helperFail[ac] == 0) continue;

    std::cout << "AC_" << AcName(ac) << ":\n";
    std::cout << "  Successes:         " << helperSucc[ac] << "\n";
    std::cout << "  Throughput:        " << hThr << " Mbps\n";
    std::cout << "  Packet Loss:       " << hLoss << " %\n";
    std::cout << "  Avg Retx/MPDU:     " << avgRetx << "\n";
    std::cout << "  Avg Queue Delay:   " << avgQueue << " us (Enqueue->TxStart)\n";
    std::cout << "  Avg Access Delay:  " << avgAccess << " us (TxStart->Ack)\n";
    std::cout << "  Avg MAC Delay:     " << avgMac << " us (Total: Enqueue->Ack)\n\n";
  }

  std::cout << "--- Per-AC Failure Statistics ---\n";
  for (int ac = 0; ac < 4; ++ac) {
    if (helperFail[ac] == 0) continue;
    std::cout << "AC_" << AcName(ac) << " Failures: " << helperFail[ac] << "\n";
  }

  // Print failure reasons
  if (!failByReason.empty()) {
    std::cout << "\n--- Failure Reasons by AC ---\n";
    for (const auto& [key, count] : failByReason) {
      uint8_t ac = key.first;
      WifiMacDropReason reason = key.second;
      std::string reasonStr;
      switch (reason) {
        case WIFI_MAC_DROP_REACHED_RETRY_LIMIT: reasonStr = "RETRY_LIMIT"; break;
        case WIFI_MAC_DROP_FAILED_ENQUEUE: reasonStr = "FAILED_ENQUEUE"; break;
        case WIFI_MAC_DROP_EXPIRED_LIFETIME: reasonStr = "EXPIRED_LIFETIME"; break;
        case WIFI_MAC_DROP_QOS_OLD_PACKET: reasonStr = "QOS_OLD_PACKET"; break;
        default: reasonStr = "OTHER"; break;
      }
      std::cout << "  AC_" << AcName(ac) << " " << reasonStr << ": " << count << "\n";
    }
  }
  std::cout << "\n";

  // =====================================================================
  // ══════ NEW: Extended P-EDCA vs Legacy Statistics (Stats 1-7) ══════
  // =====================================================================
  std::cout << "\n=== Extended P-EDCA vs Legacy Statistics ===\n";

  // ── [Stat 1] Delay partitioned by P-EDCA STAs vs Legacy STAs ──
  std::cout << "\n--- [Stat 1] Delay by STA Type ---\n";
  {
    // P-EDCA STAs
    double pAvgQ = (pedcaStaSuccCount > 0) ? (pedcaStaQueueDelay / pedcaStaSuccCount) : 0.0;
    double pAvgA = (pedcaStaSuccCount > 0) ? (pedcaStaAccessDelay / pedcaStaSuccCount) : 0.0;
    double pAvgM = (pedcaStaSuccCount > 0) ? (pedcaStaMacDelay / pedcaStaSuccCount) : 0.0;
    double pThr = (pedcaStaSuccCount * payloadSize * 8.0) / duration / 1e6;
    double pLoss = 0.0;
    if ((pedcaStaSuccCount + pedcaStaFailCount) > 0)
      pLoss = (double)pedcaStaFailCount / (double)(pedcaStaSuccCount + pedcaStaFailCount) * 100.0;

    std::cout << "P-EDCA STAs (" << nPedcaSta << " STAs):\n";
    std::cout << "  Successes:         " << pedcaStaSuccCount << "\n";
    std::cout << "  Failures:          " << pedcaStaFailCount << "\n";
    std::cout << "  Throughput:        " << pThr << " Mbps\n";
    std::cout << "  Packet Loss:       " << pLoss << " %\n";
    std::cout << "  Avg Queue Delay:   " << pAvgQ << " us\n";
    std::cout << "  Avg Access Delay:  " << pAvgA << " us\n";
    std::cout << "  Avg MAC Delay:     " << pAvgM << " us\n";

    // Median and P95 for P-EDCA STAs
    if (!pedcaStaMacDelayVec.empty()) {
      std::sort(pedcaStaMacDelayVec.begin(), pedcaStaMacDelayVec.end());
      size_t n = pedcaStaMacDelayVec.size();
      double median = pedcaStaMacDelayVec[n / 2];
      double p95 = pedcaStaMacDelayVec[(size_t)(n * 0.95)];
      double p99 = pedcaStaMacDelayVec[(size_t)(n * 0.99)];
      std::cout << "  Median MAC Delay:  " << median << " us\n";
      std::cout << "  P95 MAC Delay:     " << p95 << " us\n";
      std::cout << "  P99 MAC Delay:     " << p99 << " us\n";
    }

    // Legacy STAs
    double lAvgQ = (legacyStaSuccCount > 0) ? (legacyStaQueueDelay / legacyStaSuccCount) : 0.0;
    double lAvgA = (legacyStaSuccCount > 0) ? (legacyStaAccessDelay / legacyStaSuccCount) : 0.0;
    double lAvgM = (legacyStaSuccCount > 0) ? (legacyStaMacDelay / legacyStaSuccCount) : 0.0;
    double lThr = (legacyStaSuccCount * payloadSize * 8.0) / duration / 1e6;
    double lLoss = 0.0;
    if ((legacyStaSuccCount + legacyStaFailCount) > 0)
      lLoss = (double)legacyStaFailCount / (double)(legacyStaSuccCount + legacyStaFailCount) * 100.0;

    uint32_t nLegacy = nSta - nPedcaSta;
    std::cout << "Legacy EDCA STAs (" << nLegacy << " STAs):\n";
    std::cout << "  Successes:         " << legacyStaSuccCount << "\n";
    std::cout << "  Failures:          " << legacyStaFailCount << "\n";
    std::cout << "  Throughput:        " << lThr << " Mbps\n";
    std::cout << "  Packet Loss:       " << lLoss << " %\n";
    std::cout << "  Avg Queue Delay:   " << lAvgQ << " us\n";
    std::cout << "  Avg Access Delay:  " << lAvgA << " us\n";
    std::cout << "  Avg MAC Delay:     " << lAvgM << " us\n";

    // Median and P95 for Legacy STAs
    if (!legacyStaMacDelayVec.empty()) {
      std::sort(legacyStaMacDelayVec.begin(), legacyStaMacDelayVec.end());
      size_t n = legacyStaMacDelayVec.size();
      double median = legacyStaMacDelayVec[n / 2];
      double p95 = legacyStaMacDelayVec[(size_t)(n * 0.95)];
      double p99 = legacyStaMacDelayVec[(size_t)(n * 0.99)];
      std::cout << "  Median MAC Delay:  " << median << " us\n";
      std::cout << "  P95 MAC Delay:     " << p95 << " us\n";
      std::cout << "  P99 MAC Delay:     " << p99 << " us\n";
    }
  }

  // ── [Stat 2] Per-STA P-EDCA attempt count (DS-CTS sent) ──
  std::cout << "\n--- [Stat 2] Per-STA P-EDCA Attempt Count (DS-CTS Sent) ---\n";
  for (uint32_t i = 0; i < nPedcaSta; ++i)
  {
      Ptr<WifiNetDevice> wDev = DynamicCast<WifiNetDevice>(staDevices.Get(i));
      if (!wDev) continue;
      auto qFem = DynamicCast<QosFrameExchangeManager>(wDev->GetMac()->GetFrameExchangeManager(0));
      if (!qFem) continue;
      std::cout << "  STA" << i << ": " << qFem->GetDsCtsCount() << " P-EDCA attempts\n";
  }
  std::cout << "  Total: " << totalDsCtsSent << " P-EDCA attempts across " << nPedcaSta << " P-EDCA STAs\n";
  if (nPedcaSta > 0)
      std::cout << "  Avg per P-EDCA STA: " << ((double)totalDsCtsSent / nPedcaSta) << "\n";

  // ── [Stat 3] P-EDCA one-shot success probability ──
  //    = P-EDCA successes / DS-CTS sent (fraction of P-EDCA attempts that succeed end-to-end)
  std::cout << "\n--- [Stat 3] P-EDCA One-Shot Success Probability ---\n";
  {
    double oneShotAll = (totalDsCtsSent > 0) ? ((double)totalPedcaSuccess / totalDsCtsSent) : 0.0;
    double oneShotS2 = (totalStage2TxStart > 0) ? ((double)totalPedcaSuccess / totalStage2TxStart) : 0.0;
    std::cout << "  P-EDCA Success / DS-CTS Sent:      " << totalPedcaSuccess << " / " << totalDsCtsSent
              << " = " << (oneShotAll * 100.0) << " %\n";
    std::cout << "  P-EDCA Success / Stage2 TX Started: " << totalPedcaSuccess << " / " << totalStage2TxStart
              << " = " << (oneShotS2 * 100.0) << " %  (given RTS was actually sent)\n";
  }

  // ── [Stat 4] Legacy EDCA one-shot success ratio ──
  //    = Packets with 0 retransmissions / total successes (for legacy STAs)
  std::cout << "\n--- [Stat 4] EDCA One-Shot Success Ratio (0 retransmissions) ---\n";
  {
    double legacyOneShot = (legacyStaSuccCount > 0) ? ((double)legacyStaZeroRetx / legacyStaSuccCount * 100.0) : 0.0;
    double pedcaOneShot = (pedcaStaSuccCount > 0) ? ((double)pedcaStaZeroRetx / pedcaStaSuccCount * 100.0) : 0.0;
    std::cout << "  Legacy STAs: " << legacyStaZeroRetx << " / " << legacyStaSuccCount
              << " = " << legacyOneShot << " %\n";
    std::cout << "  P-EDCA STAs: " << pedcaStaZeroRetx << " / " << pedcaStaSuccCount
              << " = " << pedcaOneShot << " %  (MAC-level, includes both P-EDCA and EDCA paths)\n";
  }

  // ── [Stat 5] P-EDCA kickback-to-EDCA probability ──
  //    = (all P-EDCA failures) / DS-CTS sent
  std::cout << "\n--- [Stat 5] P-EDCA Kickback-to-EDCA Probability ---\n";
  {
    uint32_t totalPedcaFail = totalFailRtsCtsTimeout + totalFailRtsCollision
                            + totalFailTimingExpired + totalFailDeferral;
    double kickbackProb = (totalDsCtsSent > 0) ? ((double)totalPedcaFail / totalDsCtsSent * 100.0) : 0.0;
    std::cout << "  Total P-EDCA Failures: " << totalPedcaFail << " / " << totalDsCtsSent
              << " DS-CTS = " << kickbackProb << " %\n";
    // Breakdown
    if (totalDsCtsSent > 0) {
      std::cout << "    RTS No CTS:       " << totalFailRtsCtsTimeout << " ("
                << ((double)totalFailRtsCtsTimeout / totalDsCtsSent * 100.0) << " %)\n";
      std::cout << "    RTS Collision:    " << totalFailRtsCollision << " ("
                << ((double)totalFailRtsCollision / totalDsCtsSent * 100.0) << " %)\n";
      std::cout << "    Timing Expired:   " << totalFailTimingExpired << " ("
                << ((double)totalFailTimingExpired / totalDsCtsSent * 100.0) << " %)\n";
      std::cout << "    Deferral:         " << totalFailDeferral << " ("
                << ((double)totalFailDeferral / totalDsCtsSent * 100.0) << " %)\n";
    }
  }

  // ── [Stat 6] P-EDCA RTS collision probability ──
  //    = RTS collisions / Stage 2 TX started
  std::cout << "\n--- [Stat 6] P-EDCA RTS Collision Probability ---\n";
  {
    double collProb = (totalStage2TxStart > 0) ? ((double)totalFailRtsCollision / totalStage2TxStart * 100.0) : 0.0;
    double collProbAll = (totalDsCtsSent > 0) ? ((double)totalFailRtsCollision / totalDsCtsSent * 100.0) : 0.0;
    std::cout << "  RTS Collision / Stage2 TX Started: " << totalFailRtsCollision << " / " << totalStage2TxStart
              << " = " << collProb << " %\n";
    std::cout << "  RTS Collision / DS-CTS Sent:       " << totalFailRtsCollision << " / " << totalDsCtsSent
              << " = " << collProbAll << " %\n";
  }

  // ── [Stat 7] P-EDCA RTS attempts per successful P-EDCA TX ──
  //    Since each P-EDCA cycle sends exactly 1 DS-CTS + 1 RTS, and on failure falls back to EDCA,
  //    this equals DS-CTS sent / P-EDCA successes (how many P-EDCA cycles needed per success)
  std::cout << "\n--- [Stat 7] P-EDCA Attempts per Successful P-EDCA TX ---\n";
  {
    double attemptsPerSuccess = (totalPedcaSuccess > 0) ? ((double)totalDsCtsSent / totalPedcaSuccess) : 0.0;
    double s2PerSuccess = (totalPedcaSuccess > 0) ? ((double)totalStage2TxStart / totalPedcaSuccess) : 0.0;
    std::cout << "  DS-CTS Sent / P-EDCA Success: " << totalDsCtsSent << " / " << totalPedcaSuccess
              << " = " << attemptsPerSuccess << " attempts/success\n";
    std::cout << "  Stage2 TX / P-EDCA Success:   " << totalStage2TxStart << " / " << totalPedcaSuccess
              << " = " << s2PerSuccess << " RTS/success\n";
  }

  std::cout << "\n";

  // VO delay PDF for plotting (x=delay_us, y=pdf_per_us)
  
  if (voicePdfBinUs == 0) {
    voicePdfBinUs = 50;
  }
  std::cout << "--- VO Delay PDF (MAC Delay) ---\n";
  std::cout << "bin_start_us,bin_end_us,bin_mid_us,pdf_per_us,probability,count\n";
  std::ofstream voicePdfCsv(voicePdfOutput, std::ios::out | std::ios::trunc);
  if (voicePdfCsv.is_open()) {
    voicePdfCsv << "bin_start_us,bin_end_us,bin_mid_us,pdf_per_us,probability,count\n";
  }
  if (!voiceMacDelayUs.empty()) {
    double maxDelayUs = *std::max_element(voiceMacDelayUs.begin(), voiceMacDelayUs.end());
    uint32_t numBins = static_cast<uint32_t>(maxDelayUs / voicePdfBinUs) + 1;
    std::vector<uint64_t> hist(numBins, 0);
    for (double d : voiceMacDelayUs) {
      uint32_t idx = static_cast<uint32_t>(d / voicePdfBinUs);
      if (idx >= numBins) {
        idx = numBins - 1;
      }
      hist[idx]++;
    }
    const double sampleCount = static_cast<double>(voiceMacDelayUs.size());
    for (uint32_t i = 0; i < numBins; ++i) {
      if (hist[i] == 0) {
        continue;
      }
      double binStart = static_cast<double>(i * voicePdfBinUs);
      double binEnd = binStart + static_cast<double>(voicePdfBinUs);
      double binMid = (binStart + binEnd) / 2.0;
      double probability = static_cast<double>(hist[i]) / sampleCount;
      double pdf = probability / static_cast<double>(voicePdfBinUs);
      std::cout << binStart << "," << binEnd << "," << binMid << "," << pdf << ","
                << probability << "," << hist[i] << "\n";
      if (voicePdfCsv.is_open()) {
        voicePdfCsv << binStart << "," << binEnd << "," << binMid << "," << pdf << ","
                    << probability << "," << hist[i] << "\n";
      }
    }
  }
  if (voicePdfCsv.is_open()) {
    voicePdfCsv.close();
    std::cout << "VO Delay PDF CSV saved: " << voicePdfOutput << "\n";
  } else {
    std::cout << "VO Delay PDF CSV save failed: " << voicePdfOutput << "\n";
  }
  std::cout << "\n";

  // ── P-EDCA STA Delay Histogram CSV ──
  auto writeDelayHistCsv = [&](const std::vector<double>& delayVec,
                               const std::string& outputPath,
                               const std::string& label) {
    if (outputPath.empty() || delayVec.empty()) return;
    std::ofstream csv(outputPath, std::ios::out | std::ios::trunc);
    if (!csv.is_open()) {
      std::cout << label << " CSV save failed: " << outputPath << "\n";
      return;
    }
    csv << "bin_start_us,bin_end_us,bin_mid_us,pdf_per_us,probability,count\n";
    double maxD = *std::max_element(delayVec.begin(), delayVec.end());
    uint32_t nBins = static_cast<uint32_t>(maxD / voicePdfBinUs) + 1;
    std::vector<uint64_t> h(nBins, 0);
    for (double d : delayVec) {
      uint32_t idx = static_cast<uint32_t>(d / voicePdfBinUs);
      if (idx >= nBins) idx = nBins - 1;
      h[idx]++;
    }
    double total = static_cast<double>(delayVec.size());
    for (uint32_t i = 0; i < nBins; ++i) {
      if (h[i] == 0) continue;
      double bStart = static_cast<double>(i * voicePdfBinUs);
      double bEnd = bStart + static_cast<double>(voicePdfBinUs);
      double bMid = (bStart + bEnd) / 2.0;
      double prob = static_cast<double>(h[i]) / total;
      double pdf = prob / static_cast<double>(voicePdfBinUs);
      csv << bStart << "," << bEnd << "," << bMid << "," << pdf << ","
          << prob << "," << h[i] << "\n";
    }
    csv.close();
    std::cout << label << " CSV saved: " << outputPath << "\n";
  };

  writeDelayHistCsv(pedcaStaMacDelayVec, pedcaStaDelayOutput, "P-EDCA STA Delay PDF");
  writeDelayHistCsv(legacyStaMacDelayVec, legacyStaDelayOutput, "Legacy STA Delay PDF");

  // ── Machine-parseable extended stats (for Python) ──
  std::cout << "--- EXTENDED_STATS_BEGIN ---\n";
  std::cout << "PEDCA_STA_SUCC_COUNT: " << pedcaStaSuccCount << "\n";
  std::cout << "PEDCA_STA_FAIL_COUNT: " << pedcaStaFailCount << "\n";
  std::cout << "PEDCA_STA_ZERO_RETX: " << pedcaStaZeroRetx << "\n";
  std::cout << "LEGACY_STA_SUCC_COUNT: " << legacyStaSuccCount << "\n";
  std::cout << "LEGACY_STA_FAIL_COUNT: " << legacyStaFailCount << "\n";
  std::cout << "LEGACY_STA_ZERO_RETX: " << legacyStaZeroRetx << "\n";
  if (pedcaStaSuccCount > 0) {
    std::cout << "PEDCA_STA_AVG_MAC_DELAY: " << (pedcaStaMacDelay / pedcaStaSuccCount) << "\n";
    std::cout << "PEDCA_STA_AVG_QUEUE_DELAY: " << (pedcaStaQueueDelay / pedcaStaSuccCount) << "\n";
    std::cout << "PEDCA_STA_AVG_ACCESS_DELAY: " << (pedcaStaAccessDelay / pedcaStaSuccCount) << "\n";
  }
  if (legacyStaSuccCount > 0) {
    std::cout << "LEGACY_STA_AVG_MAC_DELAY: " << (legacyStaMacDelay / legacyStaSuccCount) << "\n";
    std::cout << "LEGACY_STA_AVG_QUEUE_DELAY: " << (legacyStaQueueDelay / legacyStaSuccCount) << "\n";
    std::cout << "LEGACY_STA_AVG_ACCESS_DELAY: " << (legacyStaAccessDelay / legacyStaSuccCount) << "\n";
  }
  std::cout << "--- EXTENDED_STATS_END ---\n";

  if (g_trajCsv.is_open()) {
    g_trajCsv.close();
  }

  Simulator::Destroy();

  // Restore std::clog and close log file
  if (originalClogBuf) {
    std::clog.rdbuf(originalClogBuf);
  }
  if (clogStream.is_open()) {
    clogStream.close();
  }

  return 0;
}
