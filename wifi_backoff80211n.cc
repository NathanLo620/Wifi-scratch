// This file is a baseline implementation of 802.11n testing EDCA backoff
// It is based on the wifi-backoff example and extends it to test EDCA backoff
// for different ACs and different data rates.

#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/internet-module.h"
#include "ns3/wifi-module.h"
#include "ns3/mobility-module.h"
#include "ns3/applications-module.h"
#include "ns3/timestamp-tag.h"
#include "ns3/wifi-mpdu.h"
#include "ns3/wifi-mac-header.h"

#include <iostream>
#include <sstream>
#include <vector>
#include <string>
#include <unordered_map>
#include <map>
#include <set>

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("WifiBackoff80211n");

// ---------------------- Utilities ----------------------
static std::string MacToString(const Mac48Address &mac)
{
  std::ostringstream oss;
  oss << mac;
  return oss.str();
}

static uint32_t ExtractNodeId(const std::string &ctx)
{
  size_t a = ctx.find("/NodeList/");
  if (a == std::string::npos) return 0;
  a += 10;
  size_t b = ctx.find('/', a);
  if (b == std::string::npos) return 0;
  return static_cast<uint32_t>(std::stoi(ctx.substr(a, b - a)));
}

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

// TID -> AC
static uint8_t TidToAc(uint8_t tid)
{
  switch (tid)
  {
    case 0: return 0; // BE
    case 1: return 1; // BK
    case 2: return 1; // BK
    case 3: return 0; // BE
    case 4: return 2; // VI
    case 5: return 2; // VI
    case 6: return 3; // VO
    case 7: return 3; // VO
    default: return 0;
  }
}

static uint8_t ContextToAc(const std::string& context)
{
  if (context.find("VO_Txop") != std::string::npos) return 3;
  if (context.find("VI_Txop") != std::string::npos) return 2;
  if (context.find("BK_Txop") != std::string::npos) return 1;
  return 0;
}

// ---------------------- App Header: Identify AC + Seq ----------------------
class AcSeqHeader : public Header
{
public:
  AcSeqHeader() = default;
  AcSeqHeader(uint8_t ac, uint32_t seq) : m_ac(ac), m_seq(seq) {}

  static TypeId GetTypeId()
  {
    static TypeId tid = TypeId("ns3::AcSeqHeader")
      .SetParent<Header>()
      .AddConstructor<AcSeqHeader>();
    return tid;
  }

  TypeId GetInstanceTypeId() const override { return GetTypeId(); }

  void SetAc(uint8_t ac) { m_ac = ac; }
  void SetSeq(uint32_t s) { m_seq = s; }
  uint8_t GetAc() const { return m_ac; }
  uint32_t GetSeq() const { return m_seq; }

  uint32_t GetSerializedSize() const override { return 1 + 4; }

  void Serialize(Buffer::Iterator i) const override
  {
    i.WriteU8(m_ac);
    i.WriteHtonU32(m_seq);
  }

  uint32_t Deserialize(Buffer::Iterator i) override
  {
    m_ac = i.ReadU8();
    m_seq = i.ReadNtohU32();
    return GetSerializedSize();
  }

  void Print(std::ostream &os) const override
  {
    os << "AC=" << (uint32_t)m_ac << " seq=" << m_seq;
  }

private:
  uint8_t  m_ac{0};
  uint32_t m_seq{0};
};

// ---------------------- Global Stats ----------------------
struct AcStats
{
  // App-level (stable)
  uint64_t txApp{0}; // Renamed from appTx to match usage
  uint64_t appRx{0};
  uint64_t appRxBytes{0};
  double   sumE2EUs{0.0};
  uint64_t cntE2E{0};

  // MAC-level (trace-based)
  uint64_t txMpdu{0};     // includes retries
  uint64_t txUnique{0};   // unique MPDU keys
  uint64_t rxMpdu{0};     // sniffer rx count (not unique)
  uint64_t rxBytesMac{0};

  double   sumAirUs{0.0};
  uint64_t cntAir{0};

  uint32_t minCw{999999};
  uint32_t maxCw{0};
};

static std::vector<AcStats> g_stats(4);
static double g_warmupTime = 5.0;

// For CW observation
static std::unordered_map<uint32_t, uint32_t> g_lastCwByNode;

// MAC Unique key: (SA, Seq, TID)
struct MacKey
{
  std::string sa;
  uint16_t seq;
  uint8_t tid;

  bool operator<(const MacKey& o) const
  {
    if (sa < o.sa) return true;
    if (sa > o.sa) return false;
    if (seq < o.seq) return true;
    if (seq > o.seq) return false;
    return tid < o.tid;
  }
};

static std::set<MacKey> g_macUniqueSeen;

// AirDelay key: (SA, Seq, TID)
struct AirKey
{
  std::string sa;
  uint16_t seq;
  uint8_t tid;

  bool operator<(const AirKey& o) const
  {
    if (sa < o.sa) return true;
    if (sa > o.sa) return false;
    if (seq < o.seq) return true;
    if (seq > o.seq) return false;
    return tid < o.tid;
  }
};

static std::map<AirKey, Time> g_lastTxBeginTime; // last attempt -> rx

// ---------------------- Custom App: constant-rate UDP sender with timestamp ----------------------
class AcUdpSender : public Application
{
public:
  void Setup(Ipv4Address dst, uint16_t port, uint8_t acId, uint8_t tos,
             uint32_t pktSize, DataRate rate)
  {
    m_peer = InetSocketAddress(dst, port);
    m_acId = acId;
    m_tos = tos;
    m_pktSize = pktSize;
    m_rate = rate;
  }

private:
  Ptr<Socket> m_socket;
  Address     m_peer;
  uint8_t     m_acId{0};
  uint8_t     m_tos{0};
  uint32_t    m_pktSize{1200};
  DataRate    m_rate{"1Mbps"};
  EventId     m_ev;
  bool        m_running{false};
  uint32_t    m_seq{0};

  void StartApplication() override
  {
    m_running = true;
    m_socket = Socket::CreateSocket(GetNode(), UdpSocketFactory::GetTypeId());
    // Set IP ToS for QoS mapping -> TID -> AC
    m_socket->SetIpTos(m_tos);
    m_socket->Connect(m_peer);
    SendOne();
  }

  void StopApplication() override
  {
    m_running = false;
    if (m_ev.IsRunning()) Simulator::Cancel(m_ev);
    if (m_socket) { m_socket->Close(); m_socket = nullptr; }
  }

  void SendOne()
  {
    if (!m_running) return;

    // Payload size = pktSize - header
    AcSeqHeader h(m_acId, ++m_seq);
    uint32_t hdrSize = h.GetSerializedSize();
    uint32_t payload = (m_pktSize > hdrSize) ? (m_pktSize - hdrSize) : 0;

    Ptr<Packet> p = Create<Packet>(payload);
    p->AddHeader(h);

    // AppTx timestamp
    TimestampTag ts;

    if (Simulator::Now().GetSeconds() >= g_warmupTime) {
        g_stats[m_acId].txApp++;
    }
    ts.SetTimestamp(Simulator::Now());
    p->AddPacketTag(ts);

    m_socket->Send(p);

    // Constant packet interval
    Time inter = Seconds((double)m_pktSize * 8.0 / (double)m_rate.GetBitRate());
    m_ev = Simulator::Schedule(inter, &AcUdpSender::SendOne, this);
  }
};

// Globals moved to top

// (Duplicate globals block removed)

// ---------------------- Trace Callbacks ----------------------
static void CwTraceCb(std::string context, uint32_t cw, uint8_t /*extra*/)
{
  if (Simulator::Now().GetSeconds() < g_warmupTime) return;

  uint8_t ac = ContextToAc(context);
  if (cw < g_stats[ac].minCw) g_stats[ac].minCw = cw;
  if (cw > g_stats[ac].maxCw) g_stats[ac].maxCw = cw;

  uint32_t nid = ExtractNodeId(context);
  g_lastCwByNode[nid] = cw;
}

// PHY TxBegin: count MPDU attempts, unique MPDU, and record AirDelay start
static void PhyTxBeginCb(std::string ctx, Ptr<const Packet> p, double /*txPowerW*/)
{
  WifiMacHeader hdr;
  Ptr<Packet> cp = p->Copy();
  if (!cp->PeekHeader(hdr)) return;
  if (!hdr.IsData()) return;
  if (hdr.GetAddr1().IsBroadcast()) return;

  if (Simulator::Now().GetSeconds() < g_warmupTime) return;

  uint8_t tid = 0;
  if (hdr.IsQosData()) tid = hdr.GetQosTid();
  uint8_t ac = hdr.IsQosData() ? TidToAc(tid) : 0;

  // Only uplink (STA->AP): nodeId != 0
  if (ExtractNodeId(ctx) == 0) return;

  g_stats[ac].txMpdu++;

  MacKey mk{MacToString(hdr.GetAddr2()), hdr.GetSequenceNumber(), tid};
  if (g_macUniqueSeen.insert(mk).second)
  {
    g_stats[ac].txUnique++;
  }

  AirKey ak{mk.sa, mk.seq, mk.tid};
  g_lastTxBeginTime[ak] = Simulator::Now(); // last attempt
}

// MonitorSnifferRx at AP: compute AirDelay (last TxBegin -> Rx)
static void MonitorSnifferRxCb(std::string ctx,
                               Ptr<const Packet> packet,
                               uint16_t /*channelFreqMhz*/,
                               WifiTxVector /*txVector*/,
                               MpduInfo /*mpduInfo*/,
                               SignalNoiseDbm /*signalNoise*/,
                               uint16_t /*staId*/)
{
  if (Simulator::Now().GetSeconds() < g_warmupTime) return;

  uint32_t nid = ExtractNodeId(ctx);
  if (nid != 0) return; // only AP callbacks

  WifiMacHeader hdr;
  Ptr<Packet> p = packet->Copy();
  if (!p->PeekHeader(hdr)) return;
  if (!hdr.IsData()) return;

  uint8_t tid = 0;
  if (hdr.IsQosData()) tid = hdr.GetQosTid();
  uint8_t ac = hdr.IsQosData() ? TidToAc(tid) : 0;

  g_stats[ac].rxMpdu++;
  g_stats[ac].rxBytesMac += p->GetSize();

  AirKey ak{MacToString(hdr.GetAddr2()), hdr.GetSequenceNumber(), tid};
  auto it = g_lastTxBeginTime.find(ak);
  if (it != g_lastTxBeginTime.end())
  {
    double us = (Simulator::Now() - it->second).GetMicroSeconds();
    g_stats[ac].sumAirUs += us;
    g_stats[ac].cntAir++;
    g_lastTxBeginTime.erase(it);
  }
}

// ---------------------- App-level Receiver at AP (true E2E) ----------------------
static void ApUdpRxCb(Ptr<Socket> sock)
{
  Address from;
  while (Ptr<Packet> p = sock->RecvFrom(from))
  {
    if (Simulator::Now().GetSeconds() < g_warmupTime) continue;

    // Read AC from header (robust per-flow classification)
    AcSeqHeader h;
    if (!p->PeekHeader(h)) continue;
    uint8_t ac = h.GetAc();
    if (ac > 3) ac = 0;

    // Remove header for rxBytes accounting (payload+timestamp tag doesn't change size)
    p->RemoveHeader(h);

    g_stats[ac].appRx++;
    g_stats[ac].appRxBytes += p->GetSize();

    TimestampTag ts;
    if (p->PeekPacketTag(ts))
    {
      double us = (Simulator::Now() - ts.GetTimestamp()).GetMicroSeconds();
      g_stats[ac].sumE2EUs += us;
      g_stats[ac].cntE2E++;
    }
  }
}

// ---------------------- Main ----------------------
int main(int argc, char *argv[])
{
  uint32_t    nSta    = 2;
  double      simTime = 10.0;
  std::string dataRate = "0.5Mbps";  // per-AC per-STA rate
  uint32_t    pktSize = 1200;
  bool        enableRts = true;
  uint32_t    queueMaxP = 400;
  double      warmup = 1.0;

  CommandLine cmd(__FILE__);
  cmd.AddValue("nSta", "Number of STAs", nSta);
  cmd.AddValue("sim", "Simulation time", simTime);
  cmd.AddValue("dataRate", "UDP sending rate PER AC PER STA (e.g. 1Mbps)", dataRate);
  cmd.AddValue("pkt", "Packet size (bytes)", pktSize);
  cmd.AddValue("enableRts", "Enable RTS/CTS", enableRts);
  cmd.AddValue("queueMaxP", "WifiMacQueue MaxSize in packets (e.g. 10000)", queueMaxP);
  cmd.AddValue("warmup", "Warmup time (s)", warmup);
  cmd.Parse(argc, argv);

  g_warmupTime = warmup;

  // RTS/CTS
  if (!enableRts)
    Config::SetDefault("ns3::WifiRemoteStationManager::RtsCtsThreshold", StringValue("999999"));
  else
    Config::SetDefault("ns3::WifiRemoteStationManager::RtsCtsThreshold", StringValue("0"));

  // Queue size
  {
    std::ostringstream oss;
    oss << queueMaxP << "p";
    Config::SetDefault("ns3::WifiMacQueue::MaxSize", StringValue(oss.str()));
  }

  NodeContainer ap, sta;
  ap.Create(1);
  sta.Create(nSta);

  // PHY/channel
  YansWifiChannelHelper chan = YansWifiChannelHelper::Default();
  YansWifiPhyHelper phy;
  phy.SetChannel(chan.Create());

  WifiHelper wifi;
  wifi.SetStandard(WIFI_STANDARD_80211n);
  wifi.SetRemoteStationManager("ns3::ConstantRateWifiManager",
                               "DataMode", StringValue("HtMcs7"),
                               "ControlMode", StringValue("HtMcs7"));

  WifiMacHelper mac;
  Ssid ssid("ns3-wifi-edca");

  // STA
  mac.SetType("ns3::StaWifiMac",
              "Ssid", SsidValue(ssid),
              "QosSupported", BooleanValue(true),
              "ActiveProbing", BooleanValue(false));
  NetDeviceContainer staDevs = wifi.Install(phy, mac, sta);

  // AP
  mac.SetType("ns3::ApWifiMac",
              "Ssid", SsidValue(ssid),
              "QosSupported", BooleanValue(true));
  NetDeviceContainer apDev = wifi.Install(phy, mac, ap);

  // Mobility
  MobilityHelper mob;
  Ptr<ListPositionAllocator> apPos = CreateObject<ListPositionAllocator>();
  apPos->Add(Vector(0.0, 0.0, 0.0));
  mob.SetPositionAllocator(apPos);
  mob.SetMobilityModel("ns3::ConstantPositionMobilityModel");
  mob.Install(ap);

  mob.SetPositionAllocator("ns3::RandomDiscPositionAllocator",
                           "X", StringValue("0.0"),
                           "Y", StringValue("0.0"),
                           "Rho", StringValue("ns3::UniformRandomVariable[Min=1.0|Max=5.0]"));
  mob.SetMobilityModel("ns3::ConstantPositionMobilityModel");
  mob.Install(sta);

  // Internet
  InternetStackHelper stack;
  stack.Install(ap);
  stack.Install(sta);

  Ipv4AddressHelper ip;
  ip.SetBase("10.1.1.0", "255.255.255.0");
  Ipv4InterfaceContainer apIf  = ip.Assign(apDev);
  ip.Assign(staDevs);

  // ---------------------- App: AP UDP socket receiver (true E2E) ----------------------
  uint16_t port = 5000;
  Ptr<Socket> rxSock = Socket::CreateSocket(ap.Get(0), UdpSocketFactory::GetTypeId());
  rxSock->Bind(InetSocketAddress(Ipv4Address::GetAny(), port));
  rxSock->SetRecvCallback(MakeCallback(&ApUdpRxCb));

  // ---------------------- Traces ----------------------
  // CW traces on all nodes/devices
  const char *acName[] = {"BE_Txop", "BK_Txop", "VI_Txop", "VO_Txop"};
  for (int i = 0; i < 4; ++i)
  {
    std::string pathC = "/NodeList/*/DeviceList/*/$ns3::WifiNetDevice/Mac/" +
                        std::string(acName[i]) + "/CwTrace";
    Config::Connect(pathC, MakeCallback(&CwTraceCb));
  }

  // PHY TxBegin for retries/unique at STA side
  Config::Connect("/NodeList/*/DeviceList/*/$ns3::WifiNetDevice/Phy/PhyTxBegin",
                  MakeCallback(&PhyTxBeginCb));

  // AP sniffer for AirDelay
  Config::Connect("/NodeList/0/DeviceList/*/$ns3::WifiNetDevice/Phy/MonitorSnifferRx",
                  MakeCallback(&MonitorSnifferRxCb));

  // ---------------------- Traffic: 4 flows per STA with ToS mapping ----------------------
  // You can adjust these ToS values if you want (verification via QosTid at MAC).
  struct FlowCfg { uint8_t ac; uint8_t tos; };
  FlowCfg flows[4] = {
    {0, 0x00}, // BE
    {1, 0x20}, // BK
    {2, 0xa0}, // VI
    {3, 0xc0}  // VO
  };

  Ptr<UniformRandomVariable> startRv = CreateObject<UniformRandomVariable>();
  DataRate r(dataRate);

  for (uint32_t i = 0; i < nSta; ++i)
  {
    for (auto &f : flows)
    {
      Ptr<AcUdpSender> app = CreateObject<AcUdpSender>();
      app->Setup(apIf.GetAddress(0), port, f.ac, f.tos, pktSize, r);
      sta.Get(i)->AddApplication(app);

      double st = startRv->GetValue(0.0, 1.0); // random start in [0,1]
      app->SetStartTime(Seconds(st));
      app->SetStopTime(Seconds(simTime));
      g_stats[f.ac].txApp += 0; // (real tx counted in AcUdpSender now)
    }
  }

  // AppTx counting: since sender is custom, we count tx by expected schedule?
  // Better: count tx exactly by socket send not exposed. We approximate by adding a counter in sender:
  // For simplicity here, we compute AppTx at end from rate*duration; but for correctness, keep below fix:
  // -> We'll count AppTx using PacketSink? No. We'll do explicit per-send count by a global callback.
  //
  // Practical solution: Use Udp socket Tx trace is not standardized across versions.
  // So we compute Loss at App-level as: AppRx / expectedTx. This is acceptable only if constant-rate schedule is stable.
  //
  // To avoid ambiguity, we instead compute Loss using MAC unique or AppRx only in this script.
  // If you need exact AppTx, tell me and I’ll add per-send counter into AcUdpSender via global pointer map.

  // Run
  Simulator::Stop(Seconds(simTime));
  Simulator::Run();

  // ---------------------- Results ----------------------
  double duration = simTime - g_warmupTime;
  if (duration <= 0) duration = simTime;

  std::cout << "\n=== RESULTS ===\n";
  std::cout << "nSta=" << nSta
            << "  per-AC-per-STA rate=" << dataRate
            << "  pkt=" << pktSize
            << "  RTS/CTS=" << (enableRts ? "ON" : "OFF")
            << "  warmup=" << g_warmupTime << "s\n\n";

  for (int ac = 0; ac < 4; ++ac)
  {
    // App-level throughput (payload bytes after removing AcSeqHeader)
    double appThr = (g_stats[ac].appRxBytes * 8.0) / duration / 1e6;

    // MAC-level throughput (sniffer bytes)
    double macThr = (g_stats[ac].rxBytesMac * 8.0) / duration / 1e6;

    // Retransmissions per unique MPDU
    double avgTx = (g_stats[ac].txUnique > 0) ? (double)g_stats[ac].txMpdu / (double)g_stats[ac].txUnique : 0.0;
    double avgRetries = (avgTx > 1.0) ? (avgTx - 1.0) : 0.0;

    // E2E delay (AppTx timestamp -> AP socket receive)
    double e2e = (g_stats[ac].cntE2E > 0) ? (g_stats[ac].sumE2EUs / (double)g_stats[ac].cntE2E) : 0.0;

    // AirDelay (last TxBegin -> AP sniffer Rx) [not EDCA contention delay]
    double air = (g_stats[ac].cntAir > 0) ? (g_stats[ac].sumAirUs / (double)g_stats[ac].cntAir) : 0.0;

    // Loss Calculation (App Layer)
    double loss = (g_stats[ac].txApp > 0) ? (1.0 - (double)g_stats[ac].appRx / (double)g_stats[ac].txApp) * 100.0 : 0.0;

    std::cout << "AC_" << AcName(ac) << "\n";
    std::cout << "  AppRxPkts:         " << g_stats[ac].appRx << "\n";
    std::cout << "  AppThroughput:     " << appThr << " Mbps\n";
    std::cout << "  MacThroughput:     " << macThr << " Mbps\n";
    std::cout << "  Packet Loss:       " << loss << " %\n";
    std::cout << "  Observed Min CW:   " << g_stats[ac].minCw << "\n";
    std::cout << "  Observed Max CW:   " << g_stats[ac].maxCw << "\n";
    std::cout << "  AvgRetries/MPDU:   " << avgRetries << "\n";
    std::cout << "  AvgAirDelay:       " << air << " us\n";
    std::cout << "  AvgE2E Delay:      " << e2e << " us\n\n";
  }

  Simulator::Destroy();
  return 0;
}
