/*
 * P-EDCA Test Simulation: Mixed P-EDCA and Non-P-EDCA STAs
 *
 * Test scenario to verify P-EDCA PSRC/QSRC logic with collision handling
 * 
 * Scenario:
 * - 1 Access Point (AP)
 * - Multiple STAs: Half support P-EDCA, half do not
 * - All STAs send VO (Voice) traffic at 1 Mbps uplink to AP
 * - Goal: Verify P-EDCA Stage 1/Stage 2 behavior and collision handling
 *
 * Verification Points:
 * - [P-EDCA TRIGGER CHECK] - Check QSRC/PSRC conditions
 * - [P-EDCA STAGE1] - DS-CTS transmission
 * - [P-EDCA STAGE2] - Backoff completion and data transmission
 * - [P-EDCA COLLISION] - Collision detection and PSRC increment
 * - [P-EDCA LIMIT] - PSRC limit reached
 * - [P-EDCA CW EXPANSION] - CW expansion formula application
 * - [P-EDCA SUCCESS/FAILURE] - QSRC/PSRC counter management
 */

#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/internet-module.h"
#include "ns3/wifi-module.h"
#include "ns3/mobility-module.h"
#include "ns3/applications-module.h"
#include "ns3/wifi-tx-stats-helper.h"

#include <map>

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("PedcaTest");

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

int main(int argc, char *argv[])
{
  // Simulation parameters
  uint32_t nPedcaSta = 2;     // Number of P-EDCA supporting STAs
  uint32_t nLegacySta = 2;    // Number of legacy (non-P-EDCA) STAs  
  double simTime = 10.0;      // Longer simulation to observe collisions
  std::string dataRate = "1Mbps";  // 1 Mbps per STA
  bool verbose = false;

  CommandLine cmd(__FILE__);
  cmd.AddValue("nPedca", "Number of P-EDCA STAs", nPedcaSta);
  cmd.AddValue("nLegacy", "Number of legacy STAs", nLegacySta);
  cmd.AddValue("simTime", "Simulation time in seconds", simTime);
  cmd.AddValue("dataRate", "Data rate per STA", dataRate);
  cmd.AddValue("verbose", "Enable ns-3 logging", verbose);
  cmd.Parse(argc, argv);

  if (verbose)
  {
    LogComponentEnable("PedcaTest", LOG_LEVEL_INFO);
  }

  uint32_t nSta = nPedcaSta + nLegacySta;
  std::clog << "=== P-EDCA Test Configuration ===" << std::endl;
  std::clog << "P-EDCA STAs: " << nPedcaSta << std::endl;
  std::clog << "Legacy STAs: " << nLegacySta << std::endl;
  std::clog << "Total STAs: " << nSta << std::endl;
  std::clog << "Data Rate: " << dataRate << " per STA" << std::endl;
  std::clog << "Simulation Time: " << simTime << "s" << std::endl;
  std::clog << "===================================" << std::endl;

  // Create nodes
  NodeContainer wifiApNode;
  wifiApNode.Create(1);
  
  NodeContainer wifiPedcaStaNodes;
  wifiPedcaStaNodes.Create(nPedcaSta);
  
  NodeContainer wifiLegacyStaNodes;
  wifiLegacyStaNodes.Create(nLegacySta);

  // PHY and Channel (802.11n, 5GHz)
  YansWifiChannelHelper channel = YansWifiChannelHelper::Default();
  YansWifiPhyHelper phy;
  phy.SetChannel(channel.Create());
  phy.Set("ChannelSettings", StringValue("{36, 20, BAND_5GHZ, 0}"));

  // WiFi Helper
  WifiHelper wifi;
  wifi.SetStandard(WIFI_STANDARD_80211n);
  wifi.SetRemoteStationManager("ns3::ConstantRateWifiManager",
                               "DataMode", StringValue("HtMcs7"),
                               "ControlMode", StringValue("HtMcs0"));

  WifiMacHelper mac;
  Ssid ssid = Ssid("pedca-test");

  // Configure AP (supports P-EDCA in BSS)
  mac.SetType("ns3::ApWifiMac",
              "Ssid", SsidValue(ssid),
              "QosSupported", BooleanValue(true),
              "PedcaSupported", BooleanValue(true));
  
  NetDeviceContainer apDevices = wifi.Install(phy, mac, wifiApNode);

  // Configure P-EDCA STAs
  mac.SetType("ns3::StaWifiMac",
              "Ssid", SsidValue(ssid),
              "QosSupported", BooleanValue(true),
              "PedcaSupported", BooleanValue(true),
              "ActiveProbing", BooleanValue(false));
  
  NetDeviceContainer pedcaStaDevices = wifi.Install(phy, mac, wifiPedcaStaNodes);

  // Configure Legacy STAs (no P-EDCA support)
  mac.SetType("ns3::StaWifiMac",
              "Ssid", SsidValue(ssid),
              "QosSupported", BooleanValue(true),
              "PedcaSupported", BooleanValue(false),
              "ActiveProbing", BooleanValue(false));
  
  NetDeviceContainer legacyStaDevices = wifi.Install(phy, mac, wifiLegacyStaNodes);

  // ---------------------- WifiTxStatsHelper ----------------------
  double warmupTime = 1.0;
  WifiTxStatsHelper wifiTxStats;
  wifiTxStats.Enable(apDevices);
  wifiTxStats.Enable(pedcaStaDevices);
  wifiTxStats.Enable(legacyStaDevices);
  wifiTxStats.Start(Seconds(warmupTime));
  wifiTxStats.Stop(Seconds(simTime));

  // Mobility: All STAs in a circle around AP to ensure same contention conditions
  MobilityHelper mobility;
  mobility.SetMobilityModel("ns3::ConstantPositionMobilityModel");
  
  Ptr<ListPositionAllocator> apPos = CreateObject<ListPositionAllocator>();
  apPos->Add(Vector(0.0, 0.0, 0.0)); // AP at origin
  mobility.SetPositionAllocator(apPos);
  mobility.Install(wifiApNode);
  
  // Place all STAs at 5m radius in a circle
  Ptr<ListPositionAllocator> staPos = CreateObject<ListPositionAllocator>();
  double radius = 5.0;
  for (uint32_t i = 0; i < nSta; ++i) {
      double theta = 2.0 * M_PI * i / nSta;
      staPos->Add(Vector(radius * cos(theta), radius * sin(theta), 0.0));
  }
  mobility.SetPositionAllocator(staPos);
  mobility.Install(wifiPedcaStaNodes);
  mobility.Install(wifiLegacyStaNodes);

  // Internet Stack
  InternetStackHelper stack;
  stack.Install(wifiApNode);
  stack.Install(wifiPedcaStaNodes);
  stack.Install(wifiLegacyStaNodes);

  Ipv4AddressHelper address;
  address.SetBase("192.168.1.0", "255.255.255.0");
  Ipv4InterfaceContainer apIf = address.Assign(apDevices);
  Ipv4InterfaceContainer pedcaStaIf = address.Assign(pedcaStaDevices);
  Ipv4InterfaceContainer legacyStaIf = address.Assign(legacyStaDevices);

  // Traffic: All STAs send VO traffic to AP
  uint16_t port = 9;
  
  // Server on AP
  UdpServerHelper server(port);
  ApplicationContainer serverApps = server.Install(wifiApNode.Get(0));
  serverApps.Start(Seconds(0.5));
  serverApps.Stop(Seconds(simTime));

  // UDP Clients on all STAs sending VO traffic
  // TOS = 0xC0 (CS6) maps to AC_VO
  DataRate rate(dataRate);
  uint32_t packetSize = 1000;
  double packetsPerSecond = rate.GetBitRate() / (8.0 * packetSize);
  Time interval = Seconds(1.0 / packetsPerSecond);

  // Create random start times to avoid perfect synchronization
  Ptr<UniformRandomVariable> startRv = CreateObject<UniformRandomVariable>();
  
  // P-EDCA STAs
  for (uint32_t i = 0; i < nPedcaSta; ++i) {
      UdpClientHelper client(apIf.GetAddress(0), port);
      client.SetAttribute("MaxPackets", UintegerValue(10000));
      client.SetAttribute("Interval", TimeValue(interval));
      client.SetAttribute("PacketSize", UintegerValue(packetSize));
      client.SetAttribute("Tos", UintegerValue(0xC0)); // AC_VO
      
      ApplicationContainer clientApp = client.Install(wifiPedcaStaNodes.Get(i));
      double start = 1.0 + startRv->GetValue(0.0, 0.2);
      clientApp.Start(Seconds(start));
      clientApp.Stop(Seconds(simTime));
      
      std::clog << "P-EDCA STA " << i << " starts at t=" << start << "s" << std::endl;
  }

  // Legacy STAs
  for (uint32_t i = 0; i < nLegacySta; ++i) {
      UdpClientHelper client(apIf.GetAddress(0), port);
      client.SetAttribute("MaxPackets", UintegerValue(10000));
      client.SetAttribute("Interval", TimeValue(interval));
      client.SetAttribute("PacketSize", UintegerValue(packetSize));
      client.SetAttribute("Tos", UintegerValue(0xC0)); // AC_VO
      
      ApplicationContainer clientApp = client.Install(wifiLegacyStaNodes.Get(i));
      double start = 1.0 + startRv->GetValue(0.0, 0.2);
      clientApp.Start(Seconds(start));
      clientApp.Stop(Seconds(simTime));
      
      std::clog << "Legacy STA " << i << " starts at t=" << start << "s" << std::endl;
  }

  std::clog << "\n=== Starting Simulation ===" << std::endl;
  std::clog << "Trace logs will show P-EDCA behavior:" << std::endl;
  std::clog << "  - [P-EDCA TRIGGER CHECK]" << std::endl;
  std::clog << "  - [P-EDCA STAGE1/STAGE2]" << std::endl;
  std::clog << "  - [P-EDCA COLLISION]" << std::endl;
  std::clog << "  - [P-EDCA LIMIT/CW EXPANSION]" << std::endl;
  std::clog << "  - [P-EDCA SUCCESS/FAILURE]" << std::endl;
  std::clog << "===========================\n" << std::endl;

  Simulator::Stop(Seconds(simTime));
  Simulator::Run();
  
  std::clog << "\n=== Simulation Complete ===" << std::endl;

  // ---------------------- WifiTxStatsHelper Output ----------------------
  double duration = simTime - warmupTime;
  if (duration <= 0) duration = simTime;
  uint32_t pktSize = 1000;

  std::cout << "\n=== WifiTxStatsHelper (MAC-layer) ===" << std::endl;
  std::cout << "Total Successes:       " << wifiTxStats.GetSuccesses() << std::endl;
  std::cout << "Total Failures:        " << wifiTxStats.GetFailures() << std::endl;
  std::cout << "Total Retransmissions: " << wifiTxStats.GetRetransmissions() << std::endl;

  // Helper function for AC name
  auto AcName = [](uint8_t ac) -> const char* {
    switch(ac) { case 0: return "BE"; case 1: return "BK"; case 2: return "VI"; case 3: return "VO"; default: return "?"; }
  };

  // Calculate Failure counts first
  auto failureRecs = wifiTxStats.GetFailureRecords();
  std::vector<uint64_t> helperFail(4, 0);
  for (const auto& [key, records] : failureRecs) {
    for (const auto& rec : records) {
      uint8_t ac = (rec.m_tid < 8) ? TidToAc(rec.m_tid) : 0;
      helperFail[ac]++;
    }
  }

  // Calculate Success stats
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

  std::cout << "\n--- Per-AC Statistics ---" << std::endl;
  std::cout << "AC,Throughput_Mbps,Loss_Pct,MAC_Access_Delay_us,Queue_Delay_us,Access_Delay_us,Retransmissions" << std::endl;
  for (int ac = 0; ac < 4; ++ac) {
    if (helperSucc[ac] == 0 && helperFail[ac] == 0) continue;

    double avgQueue = (helperCount[ac] > 0) ? (helperQueueDelay[ac] / helperCount[ac]) : 0.0;
    double avgAccess = (helperCount[ac] > 0) ? (helperAccessDelay[ac] / helperCount[ac]) : 0.0;
    double avgMac = (helperCount[ac] > 0) ? (helperMacDelay[ac] / helperCount[ac]) : 0.0;
    double avgRetx = (helperCount[ac] > 0) ? ((double)helperRetxs[ac] / helperCount[ac]) : 0.0;
    double hThr = (helperSucc[ac] * pktSize * 8.0) / duration / 1e6;
    double hLoss = ((helperSucc[ac] + helperFail[ac]) > 0) ? ((double)helperFail[ac] / (helperSucc[ac] + helperFail[ac]) * 100.0) : 0.0;

    std::cout << AcName(ac) << "," << hThr << "," << hLoss << "," << avgMac << "," << avgQueue << "," << avgAccess << "," << avgRetx << std::endl;
  }
  std::cout << "=== End of Statistics ===" << std::endl;
  
  Simulator::Destroy();

  return 0;
}
