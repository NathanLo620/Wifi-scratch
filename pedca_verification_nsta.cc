/*
 * P-EDCA Verification: N STA Scenario
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

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("PedcaVerificationNSta");

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
  uint32_t nSta = 100;
  double simTime = 10.0;
  std::string dataRate = "0.5Mbps";
  uint32_t payloadSize = 1000;
  bool verbose = false;
  double warmupTime = 1.0;

  CommandLine cmd(__FILE__);
  cmd.AddValue("nSta", "Number of stations", nSta);
  cmd.AddValue("simTime", "Simulation time (seconds)", simTime);
  cmd.AddValue("dataRate", "Data rate (e.g., 0.5Mbps)", dataRate);
  cmd.AddValue("verbose", "Enable logging", verbose);
  cmd.Parse(argc, argv);
  
  if (verbose) {
    LogComponentEnable("PedcaVerificationNSta", LOG_LEVEL_INFO);
  }
  
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
                               "ControlMode", StringValue("HtMcs0"));
  
  // RTS/CTS disabled
  Config::SetDefault("ns3::WifiRemoteStationManager::RtsCtsThreshold", StringValue("999999"));
  
  // Queue size: 400 packets
  Config::SetDefault("ns3::WifiMacQueue::MaxSize", StringValue("400p"));
  
  Ssid ssid = Ssid("pedca-nsta");

  // AP Setup
  WifiMacHelper mac;
  mac.SetType("ns3::ApWifiMac",
              "Ssid", SsidValue(ssid),
              "QosSupported", BooleanValue(true));
  NetDeviceContainer apDevices = wifi.Install(phy, mac, wifiApNode);
  
  // STA Setup - All with P-EDCA enabled
  NetDeviceContainer staDevices;
  for (uint32_t i = 0; i < nSta; ++i)
  {
      mac.SetType("ns3::StaWifiMac",
                  "Ssid", SsidValue(ssid),
                  "QosSupported", BooleanValue(true),
                  "PedcaSupported", BooleanValue(true),
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
  address.SetBase("192.168.1.0", "255.255.255.0");
  Ipv4InterfaceContainer apIf = address.Assign(apDevices);
  Ipv4InterfaceContainer staIf = address.Assign(staDevices);

  // Traffic: UDP Server on AP (one port per AC)
  uint16_t basePort = 9000;
  for (uint8_t ac = 0; ac < 4; ++ac) {
    UdpServerHelper server(basePort + ac);
    ApplicationContainer serverApp = server.Install(wifiApNode.Get(0));
    serverApp.Start(Seconds(0.5));
    serverApp.Stop(Seconds(simTime));
  }
  
  // Clients on STAs: Each STA sends traffic for all 4 ACs
  DataRate rate(dataRate);
  double packetsPerSecond = rate.GetBitRate() / (8.0 * payloadSize);
  Time interval = Seconds(1.0 / packetsPerSecond);
  
  Ptr<UniformRandomVariable> startRv = CreateObject<UniformRandomVariable>();
  
  struct FlowCfg { uint8_t ac; uint8_t tos; };
  FlowCfg flows[4] = {
      {0, 0x00}, // BE
      {1, 0x20}, // BK
      {2, 0xA0}, // VI
      {3, 0xC0}  // VO
  };

  for (uint32_t i = 0; i < nSta; ++i)
  {
      for (const auto& flow : flows) {
        UdpClientHelper client(apIf.GetAddress(0), basePort + flow.ac);
        client.SetAttribute("MaxPackets", UintegerValue(100000));
        client.SetAttribute("Interval", TimeValue(interval));
        client.SetAttribute("PacketSize", UintegerValue(payloadSize));
        client.SetAttribute("Tos", UintegerValue(flow.tos));
        
        ApplicationContainer clientApp = client.Install(wifiStaNodes.Get(i));
        double start = 0.5 + startRv->GetValue(0.0, 0.5);
        clientApp.Start(Seconds(start));
        clientApp.Stop(Seconds(simTime));
      }
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
