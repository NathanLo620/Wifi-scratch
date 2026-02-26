/*
 * P-EDCA Scenario: Traffic Mix Test
 *
 * Objective:
 * Test P-EDCA performance under a specific traffic mix:
 * - 1/3 BE
 * - 1/3 BK
 * - 1/6 VI
 * - 1/6 VO
 *
 * Compare VO Delay against standard EDCA.
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
#include <iomanip>

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("PedcaScenario");

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

int main(int argc, char* argv[])
{
  uint32_t nSta = 20; // Default to 20 for reasonable contention
  double simTime = 10.0;
  std::string totalDataRate = "2Mbps"; // Total rate per STA
  uint32_t payloadSize = 1000;
  bool enableRts = false;
  bool verbose = false;
  double warmupTime = 1.0;
  bool enablePedca = true;
  double voRatio = 0.1; // Default 10%
  bool singleVoNode = false;

  CommandLine cmd(__FILE__);
  cmd.AddValue("nSta", "Number of stations", nSta);
  cmd.AddValue("simTime", "Simulation time (seconds)", simTime);
  cmd.AddValue("totalDataRate", "Total Data rate per STA (e.g., 2Mbps)", totalDataRate);
  cmd.AddValue("voRatio", "Ratio of VO traffic (0.0-1.0), remaining split equally", voRatio);
  cmd.AddValue("singleVoNode", "If true, only STA 0 sends VO traffic", singleVoNode);
  cmd.AddValue("verbose", "Enable logging", verbose);
  cmd.Parse(argc, argv);
  
  if (verbose) {
    LogComponentEnable("PedcaScenario", LOG_LEVEL_INFO);
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
  
  // RTS/CTS
  if (!enableRts)
    Config::SetDefault("ns3::WifiRemoteStationManager::RtsCtsThreshold", StringValue("999999"));
  else
    Config::SetDefault("ns3::WifiRemoteStationManager::RtsCtsThreshold", StringValue("0"));
  
  // Queue size: 400 packets
  Config::SetDefault("ns3::WifiMacQueue::MaxSize", StringValue("400p"));
  
  Ssid ssid = Ssid("pedca-scenario");

  // AP Setup
  WifiMacHelper mac;
  mac.SetType("ns3::ApWifiMac",
              "Ssid", SsidValue(ssid),
              "QosSupported", BooleanValue(true));
  NetDeviceContainer apDevices = wifi.Install(phy, mac, wifiApNode);
  
  // STA Setup - P-EDCA enabled
  NetDeviceContainer staDevices;
  for (uint32_t i = 0; i < nSta; ++i)
  {
      mac.SetType("ns3::StaWifiMac",
                  "Ssid", SsidValue(ssid),
                  "QosSupported", BooleanValue(true),
                  "PedcaSupported", BooleanValue(enablePedca),
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
  
  // Place STAs randomly in a disc of radius 1~5m
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
  
  // Clients on STAs: Each STA sends traffic according to the mix
  // Mix: 1/3 BE, 1/3 BK, 1/6 VI, 1/6 VO
  DataRate rate(totalDataRate);
  uint64_t totalBitRate = rate.GetBitRate();

  // Calculate rate per AC
  uint64_t rateVO = totalBitRate * voRatio;
  uint64_t remaining = totalBitRate - rateVO;
  
  uint64_t rateBE = remaining / 3;
  uint64_t rateBK = remaining / 3;
  uint64_t rateVI = remaining - rateBE - rateBK; // Ensure sum matches exactly

  struct FlowCfg { uint8_t ac; uint8_t tos; uint64_t bitrate; };
  FlowCfg flows[4] = {
      {0, 0x00, rateBE}, // BE
      {1, 0x20, rateBK}, // BK
      {2, 0xA0, rateVI}, // VI
      {3, 0xC0, rateVO}  // VO
  };
  
  Ptr<UniformRandomVariable> startRv = CreateObject<UniformRandomVariable>();

  for (uint32_t i = 0; i < nSta; ++i)
  {
      for (const auto& flow : flows) {
        if (flow.bitrate == 0) continue;
        
        // If singleVoNode is true, only Node 0 sends VO (ac=3)
        if (singleVoNode && flow.ac == 3 && i != 0) continue;

        double packetsPerSecond = (double)flow.bitrate / (8.0 * payloadSize);
        Time interval = Seconds(1.0 / packetsPerSecond);

        UdpClientHelper client(apIf.GetAddress(0), basePort + flow.ac);
        client.SetAttribute("MaxPackets", UintegerValue(1000000));
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
  std::cout << "Scenario: " << (enablePedca ? "P-EDCA" : "EDCA") << "\n";
  std::cout << "Traffic Mix: VO=" << voRatio*100 << "%, Others=" << (1.0-voRatio)/3.0*100 << "% each (BE,BK,VI)\n";
  if (singleVoNode) std::cout << "  (Single VO Node: STA 0 only)\n";
  std::cout << "Total Rate per STA: " << totalDataRate << "\n";
  std::cout << "Stations: " << nSta << "\n\n";

  // 1. Calculate Failure Statistics
  auto failureRecs = wifiTxStats.GetFailureRecords();
  std::vector<uint64_t> helperFail(4, 0);
  
  for (const auto& [key, records] : failureRecs) {
    for (const auto& rec : records) {
      uint8_t ac = (rec.m_tid < 8) ? TidToAc(rec.m_tid) : 0;
      helperFail[ac]++;
    }
  }

  // 2. Calculate Success Statistics
  auto successRecs = wifiTxStats.GetSuccessRecords();
  std::vector<uint64_t> helperSucc(4, 0), helperRetxs(4, 0), helperCount(4, 0);
  std::vector<double> helperMacDelay(4, 0.0);

  for (const auto& [key, records] : successRecs) {
    for (const auto& rec : records) {
      uint8_t ac = (rec.m_tid < 8) ? TidToAc(rec.m_tid) : 0;
      helperSucc[ac]++;
      helperRetxs[ac] += rec.m_retransmissions;
      double queueUs = (rec.m_txStartTime - rec.m_enqueueTime).GetMicroSeconds();
      double accessUs = (rec.m_ackTime - rec.m_txStartTime).GetMicroSeconds();
      helperMacDelay[ac] += queueUs + accessUs;
      helperCount[ac]++;
    }
  }

  std::cout << std::fixed << std::setprecision(2);
  std::cout << "--- Per-AC Statistics ---\n";
  
  for (int ac = 0; ac < 4; ++ac) {
    double avgMac = (helperCount[ac]>0) ? (helperMacDelay[ac] / helperCount[ac]) : 0.0;
    double avgRetx = (helperCount[ac]>0) ? ((double)helperRetxs[ac] / helperCount[ac]) : 0.0;
    double hThr = (helperSucc[ac] * payloadSize * 8.0) / duration / 1e6;
    double hLoss = 0.0;
    if ((helperSucc[ac] + helperFail[ac]) > 0) {
      hLoss = (double)helperFail[ac] / (double)(helperSucc[ac] + helperFail[ac]) * 100.0;
    }
    
    // Only print if there is ANY activity
    if (helperSucc[ac] == 0 && helperFail[ac] == 0) continue;

    std::cout << "AC_" << AcName(ac) << ":\n";
    std::cout << "  Throughput:        " << hThr << " Mbps\n";
    std::cout << "  Packet Loss:       " << hLoss << " %\n";
    std::cout << "  Avg Retx/MPDU:     " << avgRetx << "\n";
    std::cout << "  Avg MAC Delay:     " << avgMac << " us\n\n";
  }
  
  Simulator::Destroy();
  return 0;
}
