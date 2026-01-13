/*
 * P-EDCA Test Simulation
 *
 * This script is designed to verify the P-EDCA implementation, specifically
 * the DS-CTS mechanism (approximated by CTS-to-Self) for AC_VO traffic.
 *
 * Scenario:
 * - 1 Access Point (AP)
 * - 1 Station (STA)
 * - Unicast UDP traffic from STA to AP mapped to AC_VO (Voice)
 * - P-EDCA support enabled on STA
 *
 * Verification:
 * - Check if CTS-to-Self frames are generated before VO data frames.
 * - This can be observed in the generated PCAP files.
 */

#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/internet-module.h"
#include "ns3/wifi-module.h"
#include "ns3/mobility-module.h"
#include "ns3/applications-module.h"

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("PedcaTest");

int main(int argc, char *argv[])
{
  // Simulation parameters
  uint32_t nSta = 1;
  double simTime = 2.0; // Short simulation to verify behavior
  bool verbose = true;
  bool enablePedca = true;

  CommandLine cmd(__FILE__);
  cmd.AddValue("nSta", "Number of stations", nSta);
  cmd.AddValue("simTime", "Simulation time in seconds", simTime);
  cmd.AddValue("verbose", "Enable logging", verbose);
  cmd.AddValue("enablePedca", "Enable P-EDCA support", enablePedca);
  cmd.Parse(argc, argv);

  if (verbose)
  {
    LogComponentEnable("PedcaTest", LOG_LEVEL_INFO);
    // You might want to enable logs for relevant Wifi components if needed
    // LogComponentEnable("WifiDefaultProtectionManager", LOG_LEVEL_FUNCTION);
    // LogComponentEnable("QosFrameExchangeManager", LOG_LEVEL_FUNCTION);
  }

  NS_LOG_INFO("Creating nodes...");
  NodeContainer wifiStaNodes;
  wifiStaNodes.Create(nSta);
  NodeContainer wifiApNode;
  wifiApNode.Create(1);

  // PHY and Channel
  YansWifiChannelHelper channel = YansWifiChannelHelper::Default();
  YansWifiPhyHelper phy;
  phy.SetChannel(channel.Create());

  // WiFi Mac and Helper
  WifiHelper wifi;
  wifi.SetStandard(WIFI_STANDARD_80211n); // Using 802.11n just to be modern enough
  
  // We use ConstantRate for predictable MCS
  wifi.SetRemoteStationManager("ns3::ConstantRateWifiManager",
                               "DataMode", StringValue("HtMcs7"),
                               "ControlMode", StringValue("HtMcs0"));

  WifiMacHelper mac;
  Ssid ssid = Ssid("ns3-pedca-test");

  // Configure AP
  mac.SetType("ns3::ApWifiMac",
              "Ssid", SsidValue(ssid),
              "QosSupported", BooleanValue(true)); // QoS needed for EDCA/AC_VO
  
  NetDeviceContainer apDevices = wifi.Install(phy, mac, wifiApNode);

  if (enablePedca)
  {
      // If P-EDCA is enabled, we add the PedcaSupported attribute to the Type Setup
      // We assume the attribute exists in WifiMac or StaWifiMac.
      mac.SetType("ns3::StaWifiMac",
                  "Ssid", SsidValue(ssid),
                  "QosSupported", BooleanValue(true),
                  "PedcaSupported", BooleanValue(true));
  }
  else
  {
      mac.SetType("ns3::StaWifiMac",
                  "Ssid", SsidValue(ssid),
                  "QosSupported", BooleanValue(true));
  }

  NetDeviceContainer staDevices = wifi.Install(phy, mac, wifiStaNodes);

  // Mobility
  MobilityHelper mobility;
  Ptr<ListPositionAllocator> positionAlloc = CreateObject<ListPositionAllocator>();
  positionAlloc->Add(Vector(0.0, 0.0, 0.0)); // AP
  // STAs slightly away
  for (uint32_t i = 0; i < nSta; i++)
    {
      positionAlloc->Add(Vector(5.0 + i, 0.0, 0.0));
    }
  mobility.SetPositionAllocator(positionAlloc);
  mobility.SetMobilityModel("ns3::ConstantPositionMobilityModel");
  mobility.Install(wifiApNode);
  mobility.Install(wifiStaNodes);

  // Internet Stack
  InternetStackHelper stack;
  stack.Install(wifiApNode);
  stack.Install(wifiStaNodes);

  Ipv4AddressHelper address;
  address.SetBase("192.168.1.0", "255.255.255.0");
  Ipv4InterfaceContainer staInterfaces;
  staInterfaces = address.Assign(staDevices);
  Ipv4InterfaceContainer apInterface;
  apInterface = address.Assign(apDevices);

  // Function to setup Traffic
  // We want AC_VO traffic. In ns-3, AC is mapped from IP TOS (DSCP).
  // VO (Voice) maps to AC_VO.
  // Typical mapping:
  // AC_BE: Best Effort (0x00)
  // AC_BK: Background (0x20 usually, or 0x08/0x10)
  // AC_VI: Video (0xA0)
  // AC_VO: Voice (0xC0 or 0xE0)
  
  uint16_t port = 9;
  
  // Install PacketSink on AP
  UdpServerHelper server(port);
  ApplicationContainer serverApps = server.Install(wifiApNode.Get(0));
  serverApps.Start(Seconds(0.5));
  serverApps.Stop(Seconds(simTime));

  // Install Client on STA
  // Sending to AP
  UdpClientHelper client(apInterface.GetAddress(0), port);
  client.SetAttribute("MaxPackets", UintegerValue(100));
  client.SetAttribute("Interval", TimeValue(Seconds(0.01))); // 100 pkt/s
  client.SetAttribute("PacketSize", UintegerValue(1000));
  
  // Set Type of Service (TOS) to map to AC_VO
  // 0xC0 = DSCP CS6 (Class Selector 6) -> often mapped to AC_VO
  // 0xE0 = DSCP CS7 -> Network Control -> AC_VO
  // 0xB8 = EF (Expedited Forwarding) -> AC_VO
  client.SetAttribute("Tos", UintegerValue(0xC0)); 

  ApplicationContainer clientApps = client.Install(wifiStaNodes.Get(0));
  clientApps.Start(Seconds(1.0)); // Start sending after 1s
  clientApps.Stop(Seconds(simTime));

  // Enable PCAP Tracing
  phy.SetPcapDataLinkType(WifiPhyHelper::DLT_IEEE802_11_RADIO);
  phy.EnablePcap("pedca-test", apDevices.Get(0), true);
  phy.EnablePcap("pedca-test", staDevices.Get(0), true);

  NS_LOG_INFO("Starting simulation...");
  Simulator::Stop(Seconds(simTime));
  Simulator::Run();
  Simulator::Destroy();
  NS_LOG_INFO("Done.");

  return 0;
}
