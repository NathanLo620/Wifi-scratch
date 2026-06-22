/*
 * P-EDCA Verification: N STA Scenario
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

#include <iostream>
#include <vector>
#include <map>
#include <set>
#include <algorithm>
#include <fstream>

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("PedcaVerificationNSta");

static double g_apIdleUs = 0;
static double g_warmupTime = 1.0;
static double g_simTime = 10.0;

// Per-STA TX event log for collision-source attribution
struct TxEvent {
  double timeUs;
  uint32_t nodeId;
  std::string frameType;
  uint32_t size;
};
static std::vector<TxEvent> g_txEvents;
static bool g_txLogEnabled = false;

void PhyTxBeginCallback(std::string context, Ptr<const Packet> packet, double txPowerW)
{
  if (!g_txLogEnabled) return;
  // Filter to warmup window
  double nowUs = Simulator::Now().GetMicroSeconds();
  if (nowUs < g_warmupTime * 1e6) return;
  if (nowUs > g_simTime * 1e6) return;

  // Parse node ID from context: "/NodeList/<id>/DeviceList/..."
  size_t s = context.find("/NodeList/");
  if (s == std::string::npos) return;
  s += 10;
  size_t e = context.find('/', s);
  uint32_t nodeId = std::stoi(context.substr(s, e - s));

  WifiMacHeader hdr;
  Ptr<Packet> pktCopy = packet->Copy();
  if (pktCopy->PeekHeader(hdr)) {
    std::string typeStr;
    if (hdr.IsRts()) typeStr = "RTS";
    else if (hdr.IsCts()) typeStr = "CTS";
    else if (hdr.IsAck()) typeStr = "ACK";
    else if (hdr.IsBlockAck()) typeStr = "BACK";
    else if (hdr.IsBlockAckReq()) typeStr = "BAR";
    else if (hdr.IsQosData()) {
      uint8_t tid = hdr.GetQosTid();
      typeStr = "QOSDATA_TID" + std::to_string(static_cast<int>(tid));
    }
    else if (hdr.IsData()) typeStr = "DATA";
    else if (hdr.IsBeacon()) typeStr = "BEACON";
    else if (hdr.IsMgt()) typeStr = "MGT";
    else if (hdr.IsCtl()) typeStr = "CTL";
    else typeStr = "OTHER";

    g_txEvents.push_back({nowUs, nodeId, typeStr, packet->GetSize()});
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

// ---------------------- Main ----------------------

int main(int argc, char* argv[])
{
  uint32_t nSta = 20;
  double simTime = 3.0;
  std::string dataRate = "1Mbps";
  std::string viDataRate = "1Mbps";
  uint32_t payloadSize = 1000;
  bool enableRts = true;
  bool enableAggregation = true;
  bool verbose = false;
  double warmupTime = 1.0;
  uint32_t voicePdfBinUs = 5;
  std::string voicePdfOutput = "scratch/delay_pdf/pedca_vo_delay_pdf.csv";
  std::string videoPdfOutput = "scratch/delay_pdf/pedca_vi_delay_pdf.csv";
  std::string pedcaStaDelayOutput = "";  // CSV for P-EDCA STA delay histogram (all ACs)
  std::string legacyStaDelayOutput = ""; // CSV for Legacy STA delay histogram (all ACs)
  std::string pedcaStaVoDelayOutput = "";  // CSV for P-EDCA STA VO-only delay
  std::string pedcaStaViDelayOutput = "";  // CSV for P-EDCA STA VI-only delay
  std::string legacyStaVoDelayOutput = ""; // CSV for Legacy STA VO-only delay
  std::string legacyStaViDelayOutput = ""; // CSV for Legacy STA VI-only delay
  std::string backoffLogOutput = "";     // CSV for per-DS-CTS backoff/outcome log
  std::string txEventLogOutput = "";     // CSV for per-STA PHY TX events
  double pedcaRatio = 0.05; // Fraction of STAs with P-EDCA enabled (0.0-1.0)

  CommandLine cmd(__FILE__);
  cmd.AddValue("nSta", "Number of stations", nSta);
  cmd.AddValue("simTime", "Simulation time (seconds)", simTime);
  cmd.AddValue("dataRate", "VO data rate per STA (e.g., 1Mbps)", dataRate);
  cmd.AddValue("viDataRate", "VI data rate per STA (e.g., 1Mbps)", viDataRate);
  cmd.AddValue("enableAggregation", "Enable A-MPDU/A-MSDU aggregation for all ACs", enableAggregation);
  cmd.AddValue("verbose", "Enable logging", verbose);
  cmd.AddValue("voicePdfBinUs", "Delay PDF bin width (microseconds, used for VO/VI)", voicePdfBinUs);
  cmd.AddValue("voicePdfOutput", "Output CSV file for VO delay PDF", voicePdfOutput);
  cmd.AddValue("videoPdfOutput", "Output CSV file for VI delay PDF", videoPdfOutput);
  cmd.AddValue("pedcaStaDelayOutput", "CSV for P-EDCA STA delay PDF (all ACs)", pedcaStaDelayOutput);
  cmd.AddValue("legacyStaDelayOutput", "CSV for Legacy STA delay PDF (all ACs)", legacyStaDelayOutput);
  cmd.AddValue("pedcaStaVoDelayOutput", "CSV for P-EDCA STA VO-only delay PDF", pedcaStaVoDelayOutput);
  cmd.AddValue("pedcaStaViDelayOutput", "CSV for P-EDCA STA VI-only delay PDF", pedcaStaViDelayOutput);
  cmd.AddValue("legacyStaVoDelayOutput", "CSV for Legacy STA VO-only delay PDF", legacyStaVoDelayOutput);
  cmd.AddValue("legacyStaViDelayOutput", "CSV for Legacy STA VI-only delay PDF", legacyStaViDelayOutput);
  cmd.AddValue("backoffLogOutput", "CSV for per-DS-CTS backoff/outcome log", backoffLogOutput);
  cmd.AddValue("txEventLogOutput", "CSV for per-STA PHY TX events", txEventLogOutput);
  cmd.AddValue("pedcaRatio", "Fraction of STAs with P-EDCA enabled (0.0-1.0)", pedcaRatio);
  cmd.Parse(argc, argv);
  
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
  wifi.SetStandard(WIFI_STANDARD_80211n);
  wifi.SetRemoteStationManager("ns3::ConstantRateWifiManager",
                               "DataMode", StringValue("HtMcs7"),
                               "ControlMode", StringValue("OfdmRate6Mbps"));
  
  // RTS/CTS
  if (!enableRts)
    Config::SetDefault("ns3::WifiRemoteStationManager::RtsCtsThreshold", StringValue("999999"));
  else
    Config::SetDefault("ns3::WifiRemoteStationManager::RtsCtsThreshold", StringValue("0"));
  
  // Queue size: 400 packets
  Config::SetDefault("ns3::WifiMacQueue::MaxSize", StringValue("10000p"));

  // Aggregation control (matches pedca_verification_nsta.cc).
  const uint32_t maxAmpduSize = enableAggregation ? 65535 : 0;
  const uint32_t maxAmsduSize = enableAggregation ? 7935 : 0;
  Config::SetDefault("ns3::WifiMac::VO_MaxAmpduSize", UintegerValue(maxAmpduSize));
  Config::SetDefault("ns3::WifiMac::VI_MaxAmpduSize", UintegerValue(maxAmpduSize));
  Config::SetDefault("ns3::WifiMac::BE_MaxAmpduSize", UintegerValue(maxAmpduSize));
  Config::SetDefault("ns3::WifiMac::BK_MaxAmpduSize", UintegerValue(maxAmpduSize));
  Config::SetDefault("ns3::WifiMac::VO_MaxAmsduSize", UintegerValue(maxAmsduSize));
  Config::SetDefault("ns3::WifiMac::VI_MaxAmsduSize", UintegerValue(maxAmsduSize));
  Config::SetDefault("ns3::WifiMac::BE_MaxAmsduSize", UintegerValue(maxAmsduSize));
  Config::SetDefault("ns3::WifiMac::BK_MaxAmsduSize", UintegerValue(maxAmsduSize));
  
  Ssid ssid = Ssid("wifi-backoff-vo");

  // AP Setup
  WifiMacHelper mac;
  mac.SetType("ns3::ApWifiMac",
              "Ssid", SsidValue(ssid),
              "QosSupported", BooleanValue(true));
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

  // Traffic: UDP Servers on AP (VO + VI)
  uint16_t basePort = 5000;
  constexpr uint8_t voAc = 3;
  constexpr uint8_t voTos = 0xC0;
  constexpr uint8_t viAc = 2;
  constexpr uint8_t viTos = 0xA0;

  UdpServerHelper voServer(basePort + voAc);
  ApplicationContainer voServerApp = voServer.Install(wifiApNode.Get(0));
  voServerApp.Start(Seconds(0.5));
  voServerApp.Stop(Seconds(simTime));

  UdpServerHelper viServer(basePort + viAc);
  ApplicationContainer viServerApp = viServer.Install(wifiApNode.Get(0));
  viServerApp.Start(Seconds(0.5));
  viServerApp.Stop(Seconds(simTime));

  // Clients on STAs: each STA sends both VO and VI traffic in parallel
  DataRate voRate(dataRate);
  double voPps = voRate.GetBitRate() / (8.0 * payloadSize);
  Time voInterval = Seconds(1.0 / voPps);

  DataRate viRate(viDataRate);
  double viPps = viRate.GetBitRate() / (8.0 * payloadSize);
  Time viInterval = Seconds(1.0 / viPps);

  Ptr<UniformRandomVariable> startRv = CreateObject<UniformRandomVariable>();

  for (uint32_t i = 0; i < nSta; ++i)
  {
      // VO client
      UdpClientHelper voClient(apIf.GetAddress(0), basePort + voAc);
      voClient.SetAttribute("MaxPackets", UintegerValue(100000));
      voClient.SetAttribute("Interval", TimeValue(voInterval));
      voClient.SetAttribute("PacketSize", UintegerValue(payloadSize));
      voClient.SetAttribute("Tos", UintegerValue(voTos));
      ApplicationContainer voApp = voClient.Install(wifiStaNodes.Get(i));
      double voStart = 0.5 + startRv->GetValue(0.0, 0.5);
      voApp.Start(Seconds(voStart));
      voApp.Stop(Seconds(simTime));

      // VI client (independent jitter so VI and VO don't fire at the same instant)
      UdpClientHelper viClient(apIf.GetAddress(0), basePort + viAc);
      viClient.SetAttribute("MaxPackets", UintegerValue(100000));
      viClient.SetAttribute("Interval", TimeValue(viInterval));
      viClient.SetAttribute("PacketSize", UintegerValue(payloadSize));
      viClient.SetAttribute("Tos", UintegerValue(viTos));
      ApplicationContainer viApp = viClient.Install(wifiStaNodes.Get(i));
      double viStart = 0.5 + startRv->GetValue(0.0, 0.5);
      viApp.Start(Seconds(viStart));
      viApp.Stop(Seconds(simTime));
  }

  // Connect PHY TX trace on every node (AP + STAs) for collision-source attribution
  if (!txEventLogOutput.empty()) {
    g_txLogEnabled = true;
    Config::Connect("/NodeList/*/DeviceList/*/$ns3::WifiNetDevice/Phy/PhyTxBegin",
                    MakeCallback(&PhyTxBeginCallback));
  }

  std::string apPhyStatePath = "/NodeList/" + std::to_string(wifiApNode.Get(0)->GetId()) + "/DeviceList/*/$ns3::WifiNetDevice/Phy/State/State";
  Config::Connect(apPhyStatePath, MakeCallback(&ApPhyStateTrace));



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
  std::vector<double> videoMacDelayUs;

  // [Stat 1] Delay partitioned by P-EDCA STAs vs Legacy STAs
  uint64_t pedcaStaSuccCount = 0, legacyStaSuccCount = 0;
  double pedcaStaQueueDelay = 0.0, pedcaStaAccessDelay = 0.0, pedcaStaMacDelay = 0.0;
  double legacyStaQueueDelay = 0.0, legacyStaAccessDelay = 0.0, legacyStaMacDelay = 0.0;
  std::vector<double> pedcaStaMacDelayVec, legacyStaMacDelayVec;
  // Split by STA-type × AC for VO/VI CDF comparison
  std::vector<double> pedcaStaVoMacDelayVec, pedcaStaViMacDelayVec;
  std::vector<double> legacyStaVoMacDelayVec, legacyStaViMacDelayVec;
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
      } else if (ac == viAc) {
        videoMacDelayUs.push_back(queueUs + accessUs);
      }

      // Partition by STA type
      double macUs = queueUs + accessUs;
      if (pedcaNodeIds.count(rec.m_nodeId)) {
        pedcaStaSuccCount++;
        pedcaStaQueueDelay += queueUs;
        pedcaStaAccessDelay += accessUs;
        pedcaStaMacDelay += macUs;
        pedcaStaMacDelayVec.push_back(macUs);
        if (ac == voAc) pedcaStaVoMacDelayVec.push_back(macUs);
        else if (ac == viAc) pedcaStaViMacDelayVec.push_back(macUs);
        if (rec.m_retransmissions == 0) pedcaStaZeroRetx++;
      } else if (legacyNodeIds.count(rec.m_nodeId)) {
        legacyStaSuccCount++;
        legacyStaQueueDelay += queueUs;
        legacyStaAccessDelay += accessUs;
        legacyStaMacDelay += macUs;
        legacyStaMacDelayVec.push_back(macUs);
        if (ac == voAc) legacyStaVoMacDelayVec.push_back(macUs);
        else if (ac == viAc) legacyStaViMacDelayVec.push_back(macUs);
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

  // VI delay PDF for plotting (x=delay_us, y=pdf_per_us)
  std::cout << "--- VI Delay PDF (MAC Delay) ---\n";
  std::cout << "bin_start_us,bin_end_us,bin_mid_us,pdf_per_us,probability,count\n";
  std::ofstream videoPdfCsv(videoPdfOutput, std::ios::out | std::ios::trunc);
  if (videoPdfCsv.is_open()) {
    videoPdfCsv << "bin_start_us,bin_end_us,bin_mid_us,pdf_per_us,probability,count\n";
  }
  if (!videoMacDelayUs.empty()) {
    double maxDelayUs = *std::max_element(videoMacDelayUs.begin(), videoMacDelayUs.end());
    uint32_t numBins = static_cast<uint32_t>(maxDelayUs / voicePdfBinUs) + 1;
    std::vector<uint64_t> hist(numBins, 0);
    for (double d : videoMacDelayUs) {
      uint32_t idx = static_cast<uint32_t>(d / voicePdfBinUs);
      if (idx >= numBins) {
        idx = numBins - 1;
      }
      hist[idx]++;
    }
    const double sampleCount = static_cast<double>(videoMacDelayUs.size());
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
      if (videoPdfCsv.is_open()) {
        videoPdfCsv << binStart << "," << binEnd << "," << binMid << "," << pdf << ","
                    << probability << "," << hist[i] << "\n";
      }
    }
  }
  if (videoPdfCsv.is_open()) {
    videoPdfCsv.close();
    std::cout << "VI Delay PDF CSV saved: " << videoPdfOutput << "\n";
  } else {
    std::cout << "VI Delay PDF CSV save failed: " << videoPdfOutput << "\n";
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
  writeDelayHistCsv(pedcaStaVoMacDelayVec, pedcaStaVoDelayOutput, "P-EDCA STA VO Delay PDF");
  writeDelayHistCsv(pedcaStaViMacDelayVec, pedcaStaViDelayOutput, "P-EDCA STA VI Delay PDF");
  writeDelayHistCsv(legacyStaVoMacDelayVec, legacyStaVoDelayOutput, "Legacy STA VO Delay PDF");
  writeDelayHistCsv(legacyStaViMacDelayVec, legacyStaViDelayOutput, "Legacy STA VI Delay PDF");

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

  // Per-AC (VO/VI) machine-parseable summary
  auto emitAcStats = [&](const char* prefix, uint8_t ac, std::vector<double>& delays) {
    uint64_t succ = helperSucc[ac];
    uint64_t fail = helperFail[ac];
    double thr = (succ * payloadSize * 8.0) / duration / 1e6;
    std::cout << prefix << "_SUCC_COUNT: " << succ << "\n";
    std::cout << prefix << "_FAIL_COUNT: " << fail << "\n";
    std::cout << prefix << "_THROUGHPUT_MBPS: " << thr << "\n";
    if (helperCount[ac] > 0) {
      std::cout << prefix << "_AVG_MAC_DELAY_US: " << (helperMacDelay[ac] / helperCount[ac]) << "\n";
      std::cout << prefix << "_AVG_QUEUE_DELAY_US: " << (helperQueueDelay[ac] / helperCount[ac]) << "\n";
      std::cout << prefix << "_AVG_ACCESS_DELAY_US: " << (helperAccessDelay[ac] / helperCount[ac]) << "\n";
    }
    if (!delays.empty()) {
      std::sort(delays.begin(), delays.end());
      size_t n = delays.size();
      std::cout << prefix << "_MEDIAN_MAC_DELAY_US: " << delays[n / 2] << "\n";
      std::cout << prefix << "_P95_MAC_DELAY_US: " << delays[(size_t)(n * 0.95)] << "\n";
      std::cout << prefix << "_P99_MAC_DELAY_US: " << delays[(size_t)(n * 0.99)] << "\n";
    }
  };
  emitAcStats("VO", voAc, voiceMacDelayUs);
  emitAcStats("VI", viAc, videoMacDelayUs);

  // Per-STA-type × per-AC split delay summary
  auto emitSplit = [&](const char* prefix, std::vector<double>& v) {
    std::cout << prefix << "_COUNT: " << v.size() << "\n";
    if (v.empty()) return;
    double sum = 0.0;
    for (double d : v) sum += d;
    std::cout << prefix << "_AVG_MAC_DELAY_US: " << (sum / v.size()) << "\n";
    std::sort(v.begin(), v.end());
    size_t n = v.size();
    std::cout << prefix << "_MEDIAN_MAC_DELAY_US: " << v[n / 2] << "\n";
    std::cout << prefix << "_P95_MAC_DELAY_US: " << v[(size_t)(n * 0.95)] << "\n";
    std::cout << prefix << "_P99_MAC_DELAY_US: " << v[(size_t)(n * 0.99)] << "\n";
  };
  emitSplit("PEDCA_STA_VO", pedcaStaVoMacDelayVec);
  emitSplit("PEDCA_STA_VI", pedcaStaViMacDelayVec);
  emitSplit("LEGACY_STA_VO", legacyStaVoMacDelayVec);
  emitSplit("LEGACY_STA_VI", legacyStaViMacDelayVec);
  std::cout << "--- EXTENDED_STATS_END ---\n";

  // ── Per-DS-CTS Backoff Log Dump ──
  if (!backoffLogOutput.empty()) {
    std::ofstream blog(backoffLogOutput, std::ios::out | std::ios::trunc);
    if (blog.is_open()) {
      blog << "sta_id,ds_cts_end_us,gap_us,backoff_slots,outcome\n";
      uint64_t totalRecords = 0;
      for (uint32_t i = 0; i < nPedcaSta; ++i) {
        Ptr<WifiNetDevice> wDev = DynamicCast<WifiNetDevice>(staDevices.Get(i));
        if (!wDev) continue;
        auto qFem = DynamicCast<QosFrameExchangeManager>(wDev->GetMac()->GetFrameExchangeManager(0));
        if (!qFem) continue;
        const auto& records = qFem->GetPedcaAttempts();
        for (const auto& r : records) {
          blog << i << "," << r.dsCtsEndUs << "," << r.gapUs << ","
               << r.backoffSlots << "," << r.outcome << "\n";
          totalRecords++;
        }
      }
      blog.close();
      std::cout << "Backoff log CSV saved: " << backoffLogOutput
                << " (" << totalRecords << " records)\n";
    } else {
      std::cout << "Backoff log CSV save failed: " << backoffLogOutput << "\n";
    }
  }

  // ── PHY TX events log ──
  if (!txEventLogOutput.empty()) {
    std::ofstream tlog(txEventLogOutput, std::ios::out | std::ios::trunc);
    if (tlog.is_open()) {
      tlog << "time_us,node_id,frame_type,size_bytes\n";
      for (const auto& e : g_txEvents) {
        tlog << e.timeUs << "," << e.nodeId << "," << e.frameType << "," << e.size << "\n";
      }
      tlog.close();
      std::cout << "TX event log CSV saved: " << txEventLogOutput
                << " (" << g_txEvents.size() << " events)\n";
    } else {
      std::cout << "TX event log save failed: " << txEventLogOutput << "\n";
    }
  }

  Simulator::Destroy();
  return 0;
}
