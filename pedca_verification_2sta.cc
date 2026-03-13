/*
 * P-EDCA Verification: 2 STA Scenario (Trace-Validated)
 *
 * Use Case:
 * - Verify mapping correctness: VO (TOS 0xC0) and VI (TOS 0xA0)
 * - Verify coexistence: P-EDCA STA (VO) vs Legacy EDCA STA (VI)
 * - Verify CTS behavior in a robust way (MAC-based identification, not NodeId guessing)
 *
 * Topology:
 * - 1 AP
 * - STA1: PedcaSupported=true, sends VO traffic (TOS 0xC0)
 * - STA2: PedcaSupported=false, sends VI traffic (TOS 0xA0)
 *
 * Trace Goal:
 * - Print only frames transmitted by STA1/STA2 based on TA MAC address
 * - Print CTS frames (duration) and QoS Data frames (TID)
 * - Print AP sniffer RX CTS (duration, TA/RA) to confirm who sent CTS
 */

#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/internet-module.h"
#include "ns3/wifi-module.h"
#include "ns3/mobility-module.h"
#include "ns3/applications-module.h"
#include "ns3/wifi-mac-header.h"

#include <iostream>
#include <fstream>
#include <sstream>
#include <cstring>

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("PedcaVerification2StaTrace");

// ---------- Global MAC addresses for robust identification ----------
static Mac48Address g_sta1Mac;
static Mac48Address g_sta2Mac;
static Mac48Address g_apMac;
static std::ofstream g_logFile; // Global log file stream

// ---------- P-EDCA Timing Verification ----------
// Track CTS TxEnd time to calculate gap to Data TxStart
static Time g_lastCtsTxBegin = Time(0);
static Time g_lastCtsTxEnd = Time(0);        // Attempted CTS TxEnd (may have collided)
static Time g_lastSuccessfulCtsTxEnd = Time(0);  // CTS that was successfully received (NAV set)
static bool g_waitingForDataAfterCts = false;
static bool g_ctsNeedsNavConfirmation = false;   // Flag to check if CTS needs NAV confirmation

// Debug: Track STA2 VI transmissions during P-EDCA window  
static int g_sta2ViTxDuringWindow = 0;
static Time g_lastSta2ViTxBegin = Time(0);  // Last STA2 VI TxBegin time for collision detection

static bool
IsSta1OrSta2Ta(const WifiMacHeader& hdr)
{
  // CTS and ACK only have Addr1 (RA). They do NOT have Addr2 (TA).
  // RTS, Data, Management frames have Addr2 (TA).
  if (hdr.IsCts() || hdr.IsAck() || hdr.IsBlockAck() || hdr.IsBlockAckReq())
  {
      return false; 
  }
  
  // Safe to call GetAddr2 now for other frame types
  Mac48Address ta = hdr.GetAddr2();
  return (ta == g_sta1Mac || ta == g_sta2Mac);
}

static const char*
NameFromTa(const WifiMacHeader& hdr)
{
  if (hdr.IsCts() || hdr.IsAck() || hdr.IsBlockAck() || hdr.IsBlockAckReq())
  {
    return "OTHER"; // No TA in these frames
  }

  Mac48Address ta = hdr.GetAddr2();
  if (ta == g_sta1Mac) return "STA1(P-EDCA/VO)";
  if (ta == g_sta2Mac) return "STA2(Legacy/VI)";
  if (ta == g_apMac)   return "AP";
  return "OTHER";
}

static const char*
AcNameFromTid(uint8_t tid)
{
  // Typical WMM grouping:
  // 0,3 -> BE ; 1,2 -> BK ; 4,5 -> VI ; 6,7 -> VO
  switch (tid)
  {
    case 6:
    case 7:
      return "VO";
    case 4:
    case 5:
      return "VI";
    case 0:
    case 3:
      return "BE";
    case 1:
    case 2:
      return "BK";
    default:
      return "UNKNOWN";
  }
}

// ---------- PHY TX trace (STA1/STA2 only, by TA MAC) ----------
static void
PhyTxBeginTrace(std::string /*context*/, Ptr<const Packet> p, double /*txPowerW*/)
{
  Ptr<Packet> cp = p->Copy();
  WifiMacHeader hdr;
  if (!cp->PeekHeader(hdr))
  {
    return;
  }

  // Only print frames transmitted by STA1/STA2 (avoid AP beacons/CF-End/etc confusion)
  if (!IsSta1OrSta2Ta(hdr))
  {
    return;
  }

  const Time now = Simulator::Now();

  // CTS - key for DS-CTS verification
  if (hdr.IsCts())
  {
    std::ostringstream msg;
    msg << "[CTS] t=" << now.GetMicroSeconds() << "us RA=" << hdr.GetAddr1();
    if (g_logFile.is_open()) g_logFile << msg.str() << std::endl;
    return;
  }

  // QoS Data - only VO and VI
  if (hdr.IsQosData() && hdr.IsData())
  {
    uint8_t tid = hdr.GetQosTid();
    const char* ac = AcNameFromTid(tid);
    
    // Track STA2 VI TxBegin time for collision detection with CTS
    if (hdr.GetAddr2() == g_sta2Mac && (tid == 4 || tid == 5))  // AC_VI
    {
      g_lastSta2ViTxBegin = now;
    }
    
    // Only log VO and VI traffic
    if (strcmp(ac, "VO") == 0 || strcmp(ac, "VI") == 0)
    {
      std::ostringstream msg;
      msg << "[" << NameFromTa(hdr) << "] " << ac << " Data t=" << now.GetMicroSeconds() << "us"
          << " Seq=" << hdr.GetSequenceNumber();
      if (g_logFile.is_open()) g_logFile << msg.str() << std::endl;
    }
    return;
  }
}

// ---------- PHY TX BEGIN trace with precise timing for P-EDCA verification ----------
static void
PhyTxBeginTracePedca(std::string context, Ptr<const Packet> p, double /*txPowerW*/)
{
  Ptr<Packet> cp = p->Copy();
  WifiMacHeader hdr;
  if (!cp->PeekHeader(hdr))
  {
    return;
  }
  
  const Time now = Simulator::Now();
  
  // Track CTS from STA1 (DS-CTS to self: RA = STA1 MAC)
  if (hdr.IsCts() && hdr.GetAddr1() == g_sta1Mac)
  {
    g_lastCtsTxBegin = now;
    g_waitingForDataAfterCts = true;
    g_sta2ViTxDuringWindow = 0;  // Reset STA2 VI counter
    
    std::ostringstream msg;
    msg << "[PHY-TIMING] CTS TxBegin at t=" << now.GetMicroSeconds() << "us"
        << " (waiting for VO Data)";
    if (g_logFile.is_open()) g_logFile << msg.str() << std::endl;
  }
  
  // Track STA2 VI transmissions during P-EDCA window (when waiting for VO Data)
  // Also record STA2 VI TxBegin time for collision detection with CTS
  if (hdr.IsQosData() && hdr.GetAddr2() == g_sta2Mac)
  {
    uint8_t tid = hdr.GetQosTid();
    if (tid == 4 || tid == 5) // AC_VI
    {
      g_lastSta2ViTxBegin = now;  // Record STA2 VI TxBegin time
      
      if (g_waitingForDataAfterCts) {
        g_sta2ViTxDuringWindow++;
        std::ostringstream msg;
        msg << "[DEBUG] STA2 VI TX during P-EDCA window at t=" << now.GetMicroSeconds() << "us"
            << " (count=" << g_sta2ViTxDuringWindow << ") -- This violates NAV!";
        if (g_logFile.is_open()) g_logFile << msg.str() << std::endl;
      }
    }
  }
  
  // Track VO Data from STA1 following CTS
  if (hdr.IsQosData() && hdr.GetAddr2() == g_sta1Mac && g_waitingForDataAfterCts)
  {
    uint8_t tid = hdr.GetQosTid();
    if (tid == 6 || tid == 7) // AC_VO
    {
      // Check if this CTS was successful (no collision during CTS transmission)
      bool ctsWasSuccessful = (g_lastCtsTxEnd == g_lastSuccessfulCtsTxEnd) &&
                              (g_lastCtsTxEnd > Time(0));
      
      std::ostringstream msg;
      msg << "[PHY-TIMING] VO Data TxBegin at t=" << now.GetMicroSeconds() << "us";
      
      if (ctsWasSuccessful) {
        // CTS was successful - calculate normal GAP
        int64_t gap = (now - g_lastSuccessfulCtsTxEnd).GetMicroSeconds();
        
        msg << ", CTS TxEnd at t=" << g_lastSuccessfulCtsTxEnd.GetMicroSeconds() << "us"
            << ", GAP = " << gap << "us";
        
        if (gap > 0 && gap <= 97) {
          msg << " [OK: within 97us]";
        } else {
          int64_t excessTime = gap - 97;
          msg << " [WARNING: exceeds 97us by " << excessTime << "us]"
              << " STA2_VI_TX=" << g_sta2ViTxDuringWindow;
        }
      } else {
        // CTS collided - P-EDCA cycle failed, this is recovery transmission
        int64_t gapFromCollidedCts = (now - g_lastCtsTxEnd).GetMicroSeconds();
        msg << ", [CTS COLLISION - P-EDCA failed]"
            << " CollidedCTS was at t=" << g_lastCtsTxEnd.GetMicroSeconds() << "us"
            << ", RecoveryGAP=" << gapFromCollidedCts << "us (not P-EDCA timing)";
      }
      
      if (g_logFile.is_open()) g_logFile << msg.str() << std::endl;
      
      g_waitingForDataAfterCts = false;
    }
  }
}

// ---------- PHY TX END trace to record CTS TxEnd time ----------
static void
PhyTxEndTracePedca(std::string context, Ptr<const Packet> p)
{
  Ptr<Packet> cp = p->Copy();
  WifiMacHeader hdr;
  if (!cp->PeekHeader(hdr))
  {
    return;
  }
  
  const Time now = Simulator::Now();
  
  // Track CTS TxEnd from STA1
  if (hdr.IsCts() && hdr.GetAddr1() == g_sta1Mac)
  {
    g_lastCtsTxEnd = now;
    
    // Collision detection: 
    // 1. If STA2 VI transmitted during CTS window (g_sta2ViTxDuringWindow > 0), OR
    // 2. If STA2 VI TxBegin occurred at same time as CTS TxBegin (simultaneous start)
    // Compare in microseconds to avoid nanosecond precision issues in ns3::Time
    bool simultaneousStart = (g_lastSta2ViTxBegin.GetMicroSeconds() == g_lastCtsTxBegin.GetMicroSeconds()) && 
                             (g_lastCtsTxBegin > Time(0));
    bool collisionDetected = (g_sta2ViTxDuringWindow > 0) || simultaneousStart;
    
    if (!collisionDetected) {
      g_lastSuccessfulCtsTxEnd = now;  // CTS was successful
    }
    
    Time airtime = now - g_lastCtsTxBegin;
    std::ostringstream msg;
    msg << "[PHY-TIMING] CTS TxEnd at t=" << now.GetMicroSeconds() << "us"
        << " (airtime=" << airtime.GetMicroSeconds() << "us)";
    if (collisionDetected) {
      msg << " [COLLISION:";
      if (g_sta2ViTxDuringWindow > 0) msg << " VI_during_CTS=" << g_sta2ViTxDuringWindow;
      if (simultaneousStart) msg << " simultaneous_start";
      msg << "]";
    } else {
      msg << " [SUCCESS: no collision]";
    }
    // Debug: show timing comparison
    msg << " (STA2_VI_TxBegin=" << g_lastSta2ViTxBegin.GetMicroSeconds() 
        << "us, CTS_TxBegin=" << g_lastCtsTxBegin.GetMicroSeconds() << "us)";
    if (g_logFile.is_open()) g_logFile << msg.str() << std::endl;
  }
}

// ---------- AP sniffer RX trace (disabled - using PHY-TIMING instead) ----------
static void
ApSnifferRxTrace(std::string /*context*/,
                 Ptr<const Packet> packet,
                 uint16_t /*channelFreqMhz*/,
                 WifiTxVector /*txVector*/,
                 MpduInfo /*mpduInfo*/,
                 SignalNoiseDbm /*signalNoise*/,
                 uint16_t /*staId*/)
{
  // Disabled - PHY-TIMING traces provide more accurate timing
  // CTS is already tracked by PhyTxBeginTracePedca and PhyTxEndTracePedca
}

int
main(int argc, char* argv[])
{
  uint32_t payloadSize = 1000;
  double simTime = 2.0;
  bool verbose = true;
  std::string dataRate = "HtMcs7";

  CommandLine cmd(__FILE__);
  cmd.AddValue("simTime", "Simulation time", simTime);
  cmd.AddValue("payloadSize", "UDP payload size", payloadSize);
  cmd.AddValue("dataRate", "Wi-Fi data mode", dataRate);
  cmd.Parse(argc, argv);

  if (verbose) {
    // Only enable INFO level logs - DEBUG is too noisy (includes NS_LOG_FUNCTION)
    LogComponentEnable("PedcaVerification2StaTrace", LOG_LEVEL_INFO);
    
    // Enable QosTxop INFO to see P-EDCA backoff numbers
    LogComponentEnable("QosTxop", LOG_LEVEL_INFO);
    
    // Enable ChannelAccessManager INFO to see NAV updates on STA2
    LogComponentEnable("ChannelAccessManager", LOG_LEVEL_INFO);
    
    // Enable QosFrameExchangeManager INFO to see P-EDCA collision detection
    LogComponentEnable("QosFrameExchangeManager", LOG_LEVEL_INFO);
}

  // Open Log File
  g_logFile.open("scratch/PEDCA_PCAP/pedca_debug_log.txt", std::ios::out | std::ios::trunc);
  if (!g_logFile.is_open()) {
      NS_LOG_WARN("Failed to open scratch/PEDCA_PCAP/pedca_debug_log.txt for writing!");
  } else {
      g_logFile << "--- Simulation Start ---" << std::endl;
      // Redirect NS_LOG (std::clog) to the file to capture QosFrameExchangeManager logs
      std::clog.rdbuf(g_logFile.rdbuf());
  }




  NodeContainer wifiStaNodes;
  wifiStaNodes.Create(2);
  NodeContainer wifiApNode;
  wifiApNode.Create(1);

  YansWifiChannelHelper channel = YansWifiChannelHelper::Default();
  YansWifiPhyHelper phy;
  phy.SetChannel(channel.Create());
  
  // Use 5GHz band for proper 802.11n SIFS=16µs timing
  // 2.4GHz uses ERP timing with SIFS=10µs
  phy.Set("ChannelSettings", StringValue("{36, 20, BAND_5GHZ, 0}"));

  WifiHelper wifi;
  wifi.SetStandard(WIFI_STANDARD_80211n);
  wifi.SetRemoteStationManager("ns3::ConstantRateWifiManager",
                               "DataMode", StringValue(dataRate),
                               "ControlMode", StringValue("HtMcs0"));

  WifiMacHelper mac;
  Ssid ssid = Ssid("ns3-pedca-verify");

  // 1) AP
  mac.SetType("ns3::ApWifiMac",
              "Ssid", SsidValue(ssid),
              "QosSupported", BooleanValue(true));
  NetDeviceContainer apDevices = wifi.Install(phy, mac, wifiApNode);

  // 2) STA 1 (P-EDCA capable)
  mac.SetType("ns3::StaWifiMac",
              "Ssid", SsidValue(ssid),
              "QosSupported", BooleanValue(true),
              "PedcaSupported", BooleanValue(true), // Ensuring P-EDCA is ON
              "ActiveProbing", BooleanValue(false));
  NetDeviceContainer sta1Device = wifi.Install(phy, mac, wifiStaNodes.Get(0));

  // 3) STA 2 (legacy EDCA)
  mac.SetType("ns3::StaWifiMac",
              "Ssid", SsidValue(ssid),
              "QosSupported", BooleanValue(true),
              "PedcaSupported", BooleanValue(false),
              "ActiveProbing", BooleanValue(false));
  NetDeviceContainer sta2Device = wifi.Install(phy, mac, wifiStaNodes.Get(1));

  // Mobility
  MobilityHelper mobility;
  Ptr<ListPositionAllocator> positionAlloc = CreateObject<ListPositionAllocator>();
  positionAlloc->Add(Vector(0.0, 0.0, 0.0));    // AP (0,0)
  positionAlloc->Add(Vector(5.0, 0.0, 0.0));    // STA1 (5,0)
  positionAlloc->Add(Vector(-5.0, 0.0, 0.0));   // STA2 (-5,0)
  mobility.SetPositionAllocator(positionAlloc);
  mobility.SetMobilityModel("ns3::ConstantPositionMobilityModel");
  mobility.Install(wifiApNode);
  mobility.Install(wifiStaNodes);

  // Internet
  InternetStackHelper stack;
  stack.Install(wifiApNode);
  stack.Install(wifiStaNodes);

  Ipv4AddressHelper address;
  address.SetBase("192.168.1.0", "255.255.255.0");
  address.Assign(apDevices);
  Ipv4InterfaceContainer sta1If = address.Assign(sta1Device);
  Ipv4InterfaceContainer sta2If = address.Assign(sta2Device);

  // Extract MAC addresses for robust trace filtering
  {
    Ptr<WifiNetDevice> apNd = apDevices.Get(0)->GetObject<WifiNetDevice>(); 
    Ptr<WifiNetDevice> s1Nd = sta1Device.Get(0)->GetObject<WifiNetDevice>();
    Ptr<WifiNetDevice> s2Nd = sta2Device.Get(0)->GetObject<WifiNetDevice>();

    g_apMac   = Mac48Address::ConvertFrom(apNd->GetMac()->GetAddress());
    g_sta1Mac = Mac48Address::ConvertFrom(s1Nd->GetMac()->GetAddress());
    g_sta2Mac = Mac48Address::ConvertFrom(s2Nd->GetMac()->GetAddress());

    NS_LOG_INFO("AP   MAC=" << g_apMac);
    NS_LOG_INFO("STA1 MAC=" << g_sta1Mac << " (PedcaSupported=true)");
    NS_LOG_INFO("STA2 MAC=" << g_sta2Mac << " (PedcaSupported=false)");
  }

  // Traffic Setup
  uint16_t port = 9;

  // Sink on AP
  UdpServerHelper server(port);
  ApplicationContainer serverApps = server.Install(wifiApNode.Get(0));
  serverApps.Start(Seconds(0.0));
  serverApps.Stop(Seconds(simTime + 1.0));

  // Client 1 (STA1): VO traffic, Tos=0xC0 (AC_VO)
  UdpClientHelper client1(Ipv4Address("192.168.1.1"), port);
  client1.SetAttribute("MaxPackets", UintegerValue(1000));
  client1.SetAttribute("Interval", TimeValue(Seconds(0.0005))); // 2000 pkt/s
  client1.SetAttribute("PacketSize", UintegerValue(payloadSize));
  client1.SetAttribute("Tos", UintegerValue(0xC0)); 

  ApplicationContainer clientApp1 = client1.Install(wifiStaNodes.Get(0));
  clientApp1.Start(Seconds(1.0)); // Traffic starts at 1.0s
  clientApp1.Stop(Seconds(simTime));

  // Client 2 (STA2): VI traffic, Tos=0xA0 (AC_VI)
  UdpClientHelper client2(Ipv4Address("192.168.1.1"), port);
  client2.SetAttribute("MaxPackets", UintegerValue(1000));
  client2.SetAttribute("Interval", TimeValue(Seconds(0.0005))); // 2000 pkt/s
  client2.SetAttribute("PacketSize", UintegerValue(payloadSize));
  client2.SetAttribute("Tos", UintegerValue(0xA0));

  ApplicationContainer clientApp2 = client2.Install(wifiStaNodes.Get(1));
  clientApp2.Start(Seconds(1.0));
  clientApp2.Stop(Seconds(simTime));

  // PCAP
  phy.SetPcapDataLinkType(WifiPhyHelper::DLT_IEEE802_11_RADIO);
  phy.EnablePcap("scratch/PEDCA_PCAP/pedca-verify-sta1", sta1Device.Get(0), true);
  phy.EnablePcap("scratch/PEDCA_PCAP/pedca-verify-sta2", sta2Device.Get(0), true);
  phy.EnablePcap("scratch/PEDCA_PCAP/pedca-verify-ap", apDevices.Get(0), true);

  // TRACE CONNECT
  Config::Connect("/NodeList/*/DeviceList/*/$ns3::WifiNetDevice/Phy/PhyTxBegin",
                  MakeCallback(&PhyTxBeginTrace));

  Config::Connect("/NodeList/*/DeviceList/*/$ns3::WifiNetDevice/Phy/MonitorSnifferRx",
                  MakeCallback(&ApSnifferRxTrace));

  // P-EDCA Timing Traces - Connect AFTER MAC addresses are known!
  // These use g_sta1Mac which is set above
  Config::Connect("/NodeList/*/DeviceList/*/$ns3::WifiNetDevice/Phy/PhyTxBegin",
                  MakeCallback(&PhyTxBeginTracePedca));
  Config::Connect("/NodeList/*/DeviceList/*/$ns3::WifiNetDevice/Phy/PhyTxEnd",
                  MakeCallback(&PhyTxEndTracePedca));

  NS_LOG_INFO("Starting Simulation...");
  Simulator::Stop(Seconds(simTime + 1.0));
  Simulator::Run();
  Simulator::Destroy();
  NS_LOG_INFO("Done.");

  return 0;
}