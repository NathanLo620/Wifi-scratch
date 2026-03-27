/*
 * P-EDCA Verification: N STA Scenario (RTS/CTS Enabled)
 *
 * Use Case:
 * - Scalability test: Simulate N STAs
 * - Compare Delay, Throughput, Retransmission, Packet Loss across Access Categories
 * - Traffic: 0.5 Mbps per STA, 4 ACs per STA (BE, BK, VI, VO)
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

#include <iostream>
#include <vector>
#include <map>
#include <optional>
#include <iomanip>
#include "ns3/pointer.h"

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("PedcaVerificationRts");

// Global stage‑2 debug log (append to existing pedca_stage2_stats.log)
static std::ofstream g_stage2Log;

static void
Stage2Log(const std::string& line)
{
  // Mirror to pedca_stage2_stats.log (if open) and to std::clog
  if (g_stage2Log.is_open())
  {
    g_stage2Log << line << std::endl;
  }
  std::clog << line << std::endl;
}
// Dummy class mimicking the exact memory layout of QosFrameExchangeManager 
// to safely bypass private access modifiers in simulation script.
class DummyQosFrameExchangeManager : public FrameExchangeManager
{
public:
    Ptr<QosTxop> m_edca;
    std::optional<Mac48Address> m_txopHolder;
    bool m_setQosQueueSize;
    bool m_protectSingleExchange;
    Time m_singleExchangeProtectionSurplus;
    bool m_initialFrame;
    bool m_pifsRecovery;
    EventId m_pifsRecoveryEvent;
    Ptr<Txop> m_edcaBackingOff;
    bool m_pedcaPending;
    uint8_t m_psrc;
    uint16_t m_qsrc;
};

// Helper to artificially force P-EDCA condition for testing STA A vs STA B collision
static void
ForcePedcaState(Ptr<QosFrameExchangeManager> qfem)
{
    if (qfem) {
        DummyQosFrameExchangeManager* dummy = reinterpret_cast<DummyQosFrameExchangeManager*>(PeekPointer(qfem));
        dummy->m_qsrc = 5;
        dummy->m_psrc = 0;
    }
    // Keep forcing (model increments PSRC on every DS-CTS); we want QSRC=5, PSRC=0 forever
    Simulator::Schedule(MicroSeconds(100), &ForcePedcaState, qfem);
}

static inline double
NowUs()
{
  return Simulator::Now().GetMicroSeconds();
}

static void
PhyTxBeginTrace(std::string who, Ptr<const Packet> p, double /*txPowerW*/)
{
  WifiMacHeader hdr;
  Ptr<Packet> copy = p->Copy();
  if (!copy->PeekHeader(hdr))
  {
    std::ostringstream oss;
    oss << std::fixed << std::setprecision(1)
        << "[TRACE][PHY-TX-BEGIN] t=" << NowUs() << "us " << who
        << " unknown-header size=" << p->GetSize();
    Stage2Log(oss.str());
    return;
  }

  std::string type = "OTHER";
  if (hdr.IsRts())
  {
    type = "RTS";
  }
  else if (hdr.IsCts())
  {
    // In this codebase, DS-CTS is represented as a CTS with fixed RA 00:0F:AC:47:43:00
    type = (hdr.GetAddr1() == Mac48Address("00:0F:AC:47:43:00")) ? "DS-CTS" : "CTS";
  }
  else if (hdr.IsAck())
  {
    type = "ACK";
  }
  else if (hdr.IsQosData())
  {
    type = "QOS-DATA";
  }
  else if (hdr.IsData())
  {
    type = "DATA";
  }

  std::ostringstream oss;
  oss << std::fixed << std::setprecision(1)
      << "[TRACE][PHY-TX-BEGIN] t=" << NowUs() << "us " << who
      << " " << type
      << " addr1=" << hdr.GetAddr1()
      << " addr2=" << hdr.GetAddr2()
      << " dur=" << hdr.GetDuration().GetMicroSeconds() << "us"
      << " size=" << p->GetSize();
  Stage2Log(oss.str());
}

static void
PhyRxDropTrace(std::string who, Ptr<const Packet> p, WifiPhyRxfailureReason reason)
{
  WifiMacHeader hdr;
  Ptr<Packet> copy = p->Copy();
  std::string type = "UNKNOWN";
  if (copy->PeekHeader(hdr))
  {
    if (hdr.IsRts()) type = "RTS";
    else if (hdr.IsCts()) type = (hdr.GetAddr1() == Mac48Address("00:0F:AC:47:43:00")) ? "DS-CTS" : "CTS";
    else if (hdr.IsAck()) type = "ACK";
    else if (hdr.IsQosData()) type = "QOS-DATA";
    else if (hdr.IsData()) type = "DATA";
    else type = "OTHER";
  }

  std::ostringstream oss;
  oss << std::fixed << std::setprecision(1)
      << "[TRACE][PHY-RX-DROP]  t=" << NowUs() << "us " << who
      << " type=" << type
      << " reason=" << static_cast<int>(reason)
      << " size=" << p->GetSize();
  Stage2Log(oss.str());
}

static void
CwTrace(std::string who, uint32_t cw, uint8_t linkId)
{
  std::ostringstream oss;
  oss << std::fixed << std::setprecision(1)
      << "[TRACE][CW]           t=" << NowUs() << "us " << who
      << " linkId=" << +linkId
      << " CW=" << cw;
  Stage2Log(oss.str());
}

static void
BackoffTrace(std::string who, uint32_t slots, uint8_t linkId)
{
  std::ostringstream oss;
  oss << std::fixed << std::setprecision(1)
      << "[TRACE][BACKOFF]      t=" << NowUs() << "us " << who
      << " linkId=" << +linkId
      << " slots=" << slots;
  Stage2Log(oss.str());
}

static void
CheckObserverEifs(std::string who, Ptr<WifiMac> mac)
{
  Ptr<ChannelAccessManager> cam = mac ? mac->GetChannelAccessManager(0) : nullptr;
  if (!cam)
  {
    return;
  }
  Time accessGrantStart = cam->GetAccessGrantStart();
  Time delta = accessGrantStart - Simulator::Now();
  std::ostringstream oss;
  oss << std::fixed << std::setprecision(1)
      << "[TRACE][OBSERVER]     t=" << NowUs() << "us " << who
      << " accessGrantStart-now=" << delta.GetMicroSeconds() << "us"
      << " (expect >>97us when EIFS applies)";
  Stage2Log(oss.str());
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
  uint32_t nSta = 3;
  double simTime = 10.0;
  std::string dataRate = "0.5Mbps";
  uint32_t payloadSize = 1000;
  bool verbose = true;
  double warmupTime = 0.0;

  CommandLine cmd(__FILE__);
  cmd.AddValue("nSta", "Number of stations", nSta);
  cmd.AddValue("simTime", "Simulation time (seconds)", simTime);
  cmd.AddValue("dataRate", "Data rate (e.g., 0.5Mbps)", dataRate);
  cmd.AddValue("verbose", "Enable logging", verbose);
  cmd.AddValue("warmupTime", "Warmup time for WifiTxStatsHelper (seconds)", warmupTime);
  cmd.Parse(argc, argv);

  // Safety: WifiTxStatsHelper asserts startTime <= stopTime
  if (warmupTime >= simTime)
  {
    warmupTime = 0.0;
  }

  // Open / append stage‑2 stats log
  g_stage2Log.open("scratch/pedca_stage2_stats.log", std::ios::out | std::ios::app);
  if (g_stage2Log.is_open())
  {
    g_stage2Log << "==== pedca_verification_rts.cc run (simTime=" << simTime
                << " warmupTime=" << warmupTime << ") ====" << std::endl;
  }
  
  if (verbose) {
    LogComponentEnable("PedcaVerificationRts", LOG_LEVEL_INFO);
    LogComponentEnable("QosFrameExchangeManager", LOG_LEVEL_DEBUG);
    LogComponentEnable("FrameExchangeManager", LOG_LEVEL_DEBUG);
    LogComponentEnable("ChannelAccessManager", LOG_LEVEL_INFO);
    LogComponentEnable("Txop", LOG_LEVEL_DEBUG);
  }
  
  NodeContainer wifiStaNodes;
  wifiStaNodes.Create(3);
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
                               "ControlMode", StringValue("HtMcs0"));

  // We want STA B (legacy) to always use RTS. Keep global threshold large and override only STA B.
  Config::SetDefault("ns3::WifiRemoteStationManager::RtsCtsThreshold", StringValue("99999"));

  // Queue size: 400 packets
  Config::SetDefault("ns3::WifiMacQueue::MaxSize", StringValue("400p"));
  
  Ssid ssid = Ssid("pedca-nsta");

  // AP Setup
  WifiMacHelper mac;
  mac.SetType("ns3::ApWifiMac",
              "Ssid", SsidValue(ssid),
              "QosSupported", BooleanValue(true));
  NetDeviceContainer apDevices = wifi.Install(phy, mac, wifiApNode);
  
  NetDeviceContainer staDevices;
  
  // STA 0 (P-EDCA supported)
  mac.SetType("ns3::StaWifiMac",
              "Ssid", SsidValue(ssid),
              "QosSupported", BooleanValue(true),
              "PedcaSupported", BooleanValue(true),
              "ActiveProbing", BooleanValue(false));
  staDevices.Add(wifi.Install(phy, mac, wifiStaNodes.Get(0)));

  // STA 1 (legacy, RTS enabled)
  mac.SetType("ns3::StaWifiMac",
              "Ssid", SsidValue(ssid),
              "QosSupported", BooleanValue(true),
              "PedcaSupported", BooleanValue(false),
              "ActiveProbing", BooleanValue(false));
  staDevices.Add(wifi.Install(phy, mac, wifiStaNodes.Get(1)));

  // STA 2 (旁觀路人)
  mac.SetType("ns3::StaWifiMac",
              "Ssid", SsidValue(ssid),
              "QosSupported", BooleanValue(true),
              "PedcaSupported", BooleanValue(false),
              "ActiveProbing", BooleanValue(false));
  staDevices.Add(wifi.Install(phy, mac, wifiStaNodes.Get(2)));

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
  
  // Place STAs at fixed positions to ensure collision
  Ptr<ListPositionAllocator> staPos = CreateObject<ListPositionAllocator>();
  staPos->Add(Vector(5.0, 0.0, 0.0)); // STA 0
  staPos->Add(Vector(5.0, 0.0, 0.0)); // STA 1
  staPos->Add(Vector(5.0, 0.0, 0.0)); // STA 2
  mobility.SetPositionAllocator(staPos);
  mobility.Install(wifiStaNodes);

  // Internet
  InternetStackHelper stack;
  stack.Install(wifiApNode);
  stack.Install(wifiStaNodes);

  Ipv4AddressHelper address;
  address.SetBase("192.168.1.0", "255.255.255.0");
  Ipv4InterfaceContainer apIf = address.Assign(apDevices);
  Ipv4InterfaceContainer staIf = address.Assign(staDevices);

  // Traffic: UDP Server on AP (one port per AC)
  // Traffic: UDP Server on AP (VO only)
  uint16_t basePort = 9000;
  UdpServerHelper server(basePort + 3);
  ApplicationContainer serverApp = server.Install(wifiApNode.Get(0));
  serverApp.Start(Seconds(0.5));
  serverApp.Stop(Seconds(simTime));
  
  // Clients on STAs: STA A and STA B send VO traffic, start exactly at the same time.
  DataRate rate(dataRate);
  double packetsPerSecond = rate.GetBitRate() / (8.0 * payloadSize);
  Time interval = Seconds(1.0 / packetsPerSecond);
  
  UdpClientHelper client(apIf.GetAddress(0), basePort + 3);
  client.SetAttribute("MaxPackets", UintegerValue(100000));
  client.SetAttribute("Interval", TimeValue(interval));
  client.SetAttribute("PacketSize", UintegerValue(payloadSize));
  client.SetAttribute("Tos", UintegerValue(0xC0));
  
  // Only STA A and STA B send traffic, start exactly at the same time to force collision
  ApplicationContainer appA = client.Install(wifiStaNodes.Get(0));
  ApplicationContainer appB = client.Install(wifiStaNodes.Get(1));
  // Start early so we can align the first P-EDCA DS-CTS with legacy RTS
  appA.Start(Seconds(0.11));
  appB.Start(Seconds(0.11));
  appA.Stop(Seconds(simTime));
  appB.Stop(Seconds(simTime));

  // Hack to force P-EDCA condition specifically for STA A
  Ptr<WifiNetDevice> dev0 = DynamicCast<WifiNetDevice>(wifiStaNodes.Get(0)->GetDevice(0));
  Ptr<QosFrameExchangeManager> qfem0 = DynamicCast<QosFrameExchangeManager>(dev0->GetMac()->GetFrameExchangeManager());
  // Force from early time so STA A always starts from QSRC=5/PSRC=0
  Simulator::Schedule(Seconds(0.1), &ForcePedcaState, qfem0);
  
  // Force STA A and STA B to start with CW=0 for VO so the *first* attempt strongly collides.
  // Use dynamic node IDs (avoid hardcoded /NodeList/1 /NodeList/2 bugs).
  uint32_t staAId = wifiStaNodes.Get(0)->GetId();
  uint32_t staBId = wifiStaNodes.Get(1)->GetId();
  std::string staAPath = "/NodeList/" + std::to_string(staAId) + "/DeviceList/*/$ns3::WifiNetDevice/Mac/QosTxop_VO/";
  std::string staBPath = "/NodeList/" + std::to_string(staBId) + "/DeviceList/*/$ns3::WifiNetDevice/Mac/QosTxop_VO/";

  Simulator::Schedule(Seconds(0.105), [staAPath, staBPath]() {
      Config::Set(staAPath + "MinCw", UintegerValue(0));
      Config::Set(staAPath + "MaxCw", UintegerValue(0));
      Config::Set(staBPath + "MinCw", UintegerValue(0));
      Config::Set(staBPath + "MaxCw", UintegerValue(0));
  });

  // Revert STA B back to normal so we can observe CW doubling after CTS timeout/failure.
  Simulator::Schedule(Seconds(0.2), [staBPath]() {
      Config::Set(staBPath + "MinCw", UintegerValue(7));
      Config::Set(staBPath + "MaxCw", UintegerValue(15));
  });

  // Force STA B to use RTS by setting its threshold to 0
  Ptr<WifiNetDevice> dev2 = DynamicCast<WifiNetDevice>(wifiStaNodes.Get(1)->GetDevice(0));
  Ptr<WifiRemoteStationManager> rsm2 = dev2->GetMac()->GetWifiRemoteStationManager();
  Simulator::Schedule(Seconds(0.05), [rsm2]() {
      rsm2->SetAttribute("RtsCtsThreshold", UintegerValue(0));
  });

  // ---- Traces for verification ----
  // STA A: observe DS-CTS transmission
  Ptr<WifiPhy> phyA = dev0->GetPhy();
  bool okA =
      phyA->TraceConnectWithoutContext("PhyTxBegin",
                                       MakeBoundCallback(&PhyTxBeginTrace, std::string("STA_A")));
  if (!okA)
  {
      NS_FATAL_ERROR("Failed to connect STA_A WifiPhy/PhyTxBegin trace");
  }

  // STA B: observe RTS transmission and CW/backoff evolution
  Ptr<WifiPhy> phyB = dev2->GetPhy();
  bool okB =
      phyB->TraceConnectWithoutContext("PhyTxBegin",
                                       MakeBoundCallback(&PhyTxBeginTrace, std::string("STA_B")));
  if (!okB)
  {
      NS_FATAL_ERROR("Failed to connect STA_B WifiPhy/PhyTxBegin trace");
  }

  Ptr<QosTxop> voTxopB = dev2->GetMac()->GetQosTxop(AC_VO);
  if (voTxopB)
  {
      bool okCw =
          voTxopB->TraceConnectWithoutContext("CwTrace",
                                              MakeBoundCallback(&CwTrace, std::string("STA_B(VO)")));
      bool okBo = voTxopB->TraceConnectWithoutContext("BackoffTrace",
                                                      MakeBoundCallback(&BackoffTrace, std::string("STA_B(VO)")));
      if (!okCw || !okBo)
      {
          NS_FATAL_ERROR("Failed to connect STA_B VO Txop traces (CwTrace/BackoffTrace)");
      }
  }

  // STA C: observe collision energy (Rx drops) and EIFS effect via accessGrantStart lag
  Ptr<WifiNetDevice> devC = DynamicCast<WifiNetDevice>(wifiStaNodes.Get(2)->GetDevice(0));
  Ptr<WifiPhy> phyC = devC->GetPhy();
  bool okRxDrop =
      phyC->TraceConnectWithoutContext("PhyRxDrop",
                                       MakeBoundCallback(&PhyRxDropTrace, std::string("STA_C")));
  if (!okRxDrop)
  {
      NS_FATAL_ERROR("Failed to connect STA_C WifiPhy/PhyRxDrop trace");
  }

  std::clog << "[TRACE][INIT] Connected traces. STA_A nodeId=" << staAId
            << " STA_B nodeId=" << staBId
            << " STA_C nodeId=" << wifiStaNodes.Get(2)->GetId()
            << std::endl;

  // After apps start, periodically sample the observer's access-grant start; after a collision
  // this should jump by EIFS (which is >>97us).
  Ptr<WifiMac> macC = devC->GetMac();
  // Fine-grained sampling right after the intended DS-CTS/RTS collision (~120007us).
  // We expect accessGrantStart-now to be large (EIFS) immediately after Rx error.
  for (double t = 0.120010; t < 0.120250; t += 0.000005)
  {
      Simulator::Schedule(Seconds(t), &CheckObserverEifs, std::string("STA_C"), macC);
  }

  Simulator::Stop(Seconds(simTime + 1.0));
  Simulator::Run();
  
  // ---------------------- WifiTxStatsHelper Output ----------------------
  double duration = simTime - warmupTime;
  if (duration <= 0) duration = 1.0;

  std::cout << "\n=== WifiTxStatsHelper (MAC-layer) ===\n";
  std::cout << "Total Successes:       " << wifiTxStats.GetSuccesses() << "\n";
  std::cout << "Total Failures:        " << wifiTxStats.GetFailures() << "\n";
  std::cout << "Total Retransmissions: " << wifiTxStats.GetRetransmissions() << "\n\n";

  // 1. Calculate Failure Statistics FIRST (needed for Loss calculation)
  auto failureRecs = wifiTxStats.GetFailureRecords();
  std::vector<uint64_t> helperFail(4, 0);
  std::map<std::pair<uint8_t, WifiMacDropReason>, uint64_t> failByReason;
  
  for (const auto& [key, records] : failureRecs) {
    for (const auto& rec : records) {
      uint8_t ac = (rec.m_tid < 8) ? TidToAc(rec.m_tid) : 0;
      helperFail[ac]++;
      if (rec.m_dropReason.has_value()) {
        failByReason[{ac, rec.m_dropReason.value()}]++;
      }
    }
  }

  // 2. Calculate Success Statistics and Print All
  auto successRecs = wifiTxStats.GetSuccessRecords();
  std::vector<uint64_t> helperSucc(4, 0), helperRetxs(4, 0), helperCount(4, 0);
  std::vector<double> helperQueueDelay(4, 0.0), helperAccessDelay(4, 0.0), helperMacDelay(4, 0.0);

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

  Simulator::Destroy();
  return 0;
}
