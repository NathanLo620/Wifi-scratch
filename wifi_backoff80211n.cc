#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/internet-module.h"
#include "ns3/wifi-module.h"
#include "ns3/mobility-module.h"
#include "ns3/applications-module.h"
#include "ns3/txop.h"

#include <iostream>
#include <sstream>
#include <vector>
#include <string>
#include <unordered_map>
#include <map>
#include <set>

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("WifiBackoff");

// Helper to convert Mac48Address to string
static std::string
MacToString(const Mac48Address &mac)
{
  std::ostringstream oss;
  oss << mac;
  return oss.str();
}
 
// ---------------------- Global Stat Structs ----------------------
struct AcStats
{
  uint64_t txApp{0};    // New: Application Layer Tx Count
  uint64_t txMpdu{0};
  uint64_t txUnique{0};
  uint64_t rxUnique{0};
  uint64_t rxBytes{0};
  double   sumQueueDelay{0.0};
  uint64_t countQueueDelay{0};
  double   sumAirDelay{0.0};
  uint64_t countAirDelay{0};
  double   sumE2EDelay{0.0};
  uint64_t countE2EDelay{0};
  
  uint32_t minCw{99999}; // Track observed Min CW
  uint32_t maxCw{0};
};

static std::unordered_map<uint32_t, uint32_t> g_lastCwByNode;

// 0=BE, 1=BK, 2=VI, 3=VO
static std::vector<AcStats> g_stats(4);
static double g_warmupTime = 5;

// Map TID to AC Index
// TID: 0(BE), 1(BK), 2(BK), 3(BE), 4(VI), 5(VI), 6(VO), 7(VO)
static uint8_t TidToAc(uint8_t tid) {
    switch(tid) {
        case 0: return 0; // BE
        case 1: return 1; // BK
        case 2: return 1; // BK
        case 3: return 0; // BE
        case 4: return 2; // VI
        case 5: return 2; // VI
        case 6: return 3; // VO
        case 7: return 3; // VO
        default: return 0; // BE
    }
}

static uint8_t ContextToAc(const std::string& context) {
    if (context.find("VO_Txop") != std::string::npos) return 3;
    if (context.find("VI_Txop") != std::string::npos) return 2;
    if (context.find("BK_Txop") != std::string::npos) return 1;
    return 0; // BE
}

// [FIX] Tracking by (SA, Sequence) for 802.11n retransmissions
struct TxKey
{
  std::string sa;
  uint16_t    seq;
  uint8_t     tid;
  
  bool operator<(const TxKey &o) const
  {
      if (sa < o.sa) return true;
      if (sa > o.sa) return false;
      if (seq < o.seq) return true;
      if (seq > o.seq) return false;
      return tid < o.tid;
  }
};

static std::map<uint64_t, Time> g_packetEnqueueTime; // Key: Packet UID -> Enqueue Time (True Start Time)
static std::set<uint64_t>      g_macSeenUids;       // Key: Packet UID (To count Unique MAC Tx)
// Helper to know which AC a Key belongs to
static std::map<TxKey, uint8_t>  g_keyToAc; 

// Air Delay 仍需使用 (SA, Seq, TID) 因為接收端只看得到 Header
struct AirKey
{
  std::string sa;
  uint16_t    seq;
  uint8_t     tid;

  bool operator<(const AirKey &o) const
  {
    if (sa < o.sa) return true;
    if (sa > o.sa) return false;
    if (seq < o.seq) return true;
    if (seq > o.seq) return false;
    return tid < o.tid;
  }
};

static std::map<AirKey, Time> g_airTxTime;
static std::set<AirKey> g_airRxUnique;

// ---------------------- 小工具 ----------------------
static uint32_t
ExtractNodeId(const std::string &ctx)
{
  size_t a = ctx.find("/NodeList/");
  if (a == std::string::npos)
    return 0;
  a += 10;
  size_t b = ctx.find('/', a);
  if (b == std::string::npos)
    return 0;
  return static_cast<uint32_t>(std::stoi(ctx.substr(a, b - a)));
}

[[maybe_unused]] static const char *
AcName(uint8_t ac)
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

// ---------------------- Callbacks ----------------------

static void
BackoffTraceCb(std::string context, uint32_t val, uint8_t extra)
{
  // Not used right now
}

static void
CwTraceCb(std::string context, uint32_t cw, uint8_t extra)
{
  if (Simulator::Now().GetSeconds() < g_warmupTime) return;

  uint8_t ac = ContextToAc(context);
  // cw is likely the new CW value
  if (cw < g_stats[ac].minCw) g_stats[ac].minCw = cw;
  if (cw > g_stats[ac].maxCw) g_stats[ac].maxCw = cw;
  
  uint32_t nid = ExtractNodeId(context);
  g_lastCwByNode[nid] = cw;
}

static void
AppTxCb(std::string context, Ptr<const Packet> p)
{
    if (Simulator::Now().GetSeconds() < g_warmupTime) return;
    
    // Context is /NodeList/x/ApplicationList/y/$ns3::OnOffApplication/Tx
    // We map Application Index to AC.
    // In SetupTraffic loop: App 0=BE, 1=BK, 2=VI, 3=VO
    
    // Extract Application Index
    // Context format: ".../ApplicationList/<Index>"
    size_t start = context.find("/ApplicationList/") + 17;
    std::string numStr = context.substr(start);
    
    uint32_t appIdx = 0;
    try {
        appIdx = std::stoi(numStr);
    } catch (...) {
        return; 
    }
    
    // [FIX] Use Modulo to handle multiple Stations
    // 0,1,2,3 -> Sta 0
    // 4,5,6,7 -> Sta 1 ...
    uint8_t ac = 0;
    switch(appIdx % 4) {
        case 0: ac = 0; break; // BE
        case 1: ac = 1; break; // BK
        case 2: ac = 2; break; // VI
        case 3: ac = 3; break; // VO
        default: ac = 0; break;
    }
    
    g_stats[ac].txApp++;
}

static void
EnqueueCb(std::string context, Ptr<const WifiMpdu> mpdu)
{
  if (Simulator::Now().GetSeconds() < g_warmupTime) return;

  Ptr<const Packet> p = mpdu->GetPacket();
  if (p) {
      uint64_t uid = p->GetUid();
      // Record Enqueue Time if not already recorded (first enqueue)
      if (g_packetEnqueueTime.find(uid) == g_packetEnqueueTime.end()) {
          g_packetEnqueueTime[uid] = Simulator::Now();
      }
  }
}

static void
PhyTxBeginCb(std::string ctx, Ptr<const Packet> p, double /*txPowerW*/)
{
  WifiMacHeader hdr;
  Ptr<Packet>   cp = p->Copy();
  if (!cp->PeekHeader(hdr)) return;
  if (!hdr.IsData()) return;
  if (hdr.GetAddr1().IsBroadcast()) return;

  if (Simulator::Now().GetSeconds() < g_warmupTime) return;

  TxKey key;
  key.sa = MacToString(hdr.GetAddr2());
  key.seq = hdr.GetSequenceNumber();
  key.tid = 0;
  if (hdr.IsQosData()) key.tid = hdr.GetQosTid();
  
  // Find AC
  uint8_t ac = 0;
  if (g_keyToAc.count(key)) ac = g_keyToAc[key];
  else if (hdr.IsQosData()) ac = TidToAc(hdr.GetQosTid());

  g_stats[ac].txMpdu++; // Count EVERY transmission attempt (including retries)

  // Track Unique Packets (MAC Layer Context)
  // This is used to calculate Avg Retries = (Total MPDU / Unique MAC) - 1
  uint64_t uid = p->GetUid();
  if (g_macSeenUids.find(uid) == g_macSeenUids.end())
  {
      g_macSeenUids.insert(uid);
      g_stats[ac].txUnique++;
  }
  
  if (ExtractNodeId(ctx) != 0) // Uplink only
  {
    AirKey akey;
    akey.sa  = MacToString(hdr.GetAddr2());
    akey.seq = hdr.GetSequenceNumber();
    akey.tid = key.tid;
    g_airTxTime[akey] = Simulator::Now();
  }
}

static void
MonitorSnifferRxCb(std::string key,
                   Ptr<const Packet> packet,
                   uint16_t channelFreqMhz,
                   WifiTxVector txVector,
                   MpduInfo mpduInfo,
                   SignalNoiseDbm signalNoise,
                   uint16_t staId)

{
  WifiMacHeader hdr;
  Ptr<Packet> p = packet->Copy();
  if (!p->PeekHeader(hdr)) return;
  if (!hdr.IsData()) return;

  // We only care about packets received BY the AP (Node 0)
  // Context for MonitorSnifferRx is usually "/NodeList/x/..."
  // We only connect Node 0, so this is safe. 
  // But verifying Addr1 (RA) is AP's Mac is better.
  // Converting AP Mac to string is expensive? 
  // Let's rely on Connect path "/NodeList/0/..."
  
  if (Simulator::Now().GetSeconds() < g_warmupTime) return;

  uint32_t nid = ExtractNodeId(key);
  // Ensure we are on Node 0
  if (nid != 0) return;

  // Identify AC
  uint8_t ac = 0;
  if (hdr.IsQosData()) ac = TidToAc(hdr.GetQosTid());
  
  g_stats[ac].rxBytes += p->GetSize();

  AirKey akey;
  akey.sa  = MacToString(hdr.GetAddr2());
  akey.seq = hdr.GetSequenceNumber();
  akey.tid = 0;
  if (hdr.IsQosData()) akey.tid = hdr.GetQosTid();

  // Calculate Air Delay
  auto it = g_airTxTime.find(akey);
  if (it != g_airTxTime.end()) {
      double dUs = (Simulator::Now() - it->second).GetMicroSeconds();
      g_stats[ac].sumAirDelay += dUs;
      g_stats[ac].countAirDelay++;
      g_airTxTime.erase(it);
  }

  // Calculate E2E Delay using Enqueue Time
  uint64_t uid = packet->GetUid(); 
  
  auto itEnq = g_packetEnqueueTime.find(uid);

  if (itEnq != g_packetEnqueueTime.end()) {
      double e2eUs = (Simulator::Now() - itEnq->second).GetMicroSeconds();
      g_stats[ac].sumE2EDelay += e2eUs;
      g_stats[ac].countE2EDelay++;
      
      // We can erase it to save memory, assuming 1-to-1 mapping
      g_packetEnqueueTime.erase(itEnq);
  }

  // g_airRxUnique.insert(akey);
  g_stats[ac].rxUnique++;
}

// ---------------------- Main ----------------------
int
main(int argc, char *argv[])
{
  uint32_t    nSta    = 100;
  double      simTime = 100.0;
  std::string dataRate = "1Mbps";
  uint32_t    pktSize = 1200;
  bool        enableRts = false;

  CommandLine cmd(__FILE__);
  cmd.AddValue("nSta", "Number of STAs", nSta);
  cmd.AddValue("sim", "Simulation time", simTime);
  cmd.AddValue("dataRate", "UDP sending rate (e.g. 1Mbps)", dataRate);
  cmd.AddValue("pkt", "Packet size (bytes)", pktSize);
  cmd.AddValue("enableRts", "Enable RTS/CTS", enableRts);
  cmd.Parse(argc, argv);

  // Create Nodes
  NodeContainer ap, sta;
  ap.Create(1);
  sta.Create(nSta);

  // Wi-Fi Setup
  YansWifiChannelHelper chan = YansWifiChannelHelper::Default();
  YansWifiPhyHelper     phy;
  phy.SetChannel(chan.Create());

  WifiHelper wifi;
  if(!enableRts)
    Config::SetDefault ("ns3::WifiRemoteStationManager::RtsCtsThreshold", StringValue ("999999"));
  else
    Config::SetDefault ("ns3::WifiRemoteStationManager::RtsCtsThreshold", StringValue ("0"));

  wifi.SetStandard(WIFI_STANDARD_80211n);
  wifi.SetRemoteStationManager("ns3::ConstantRateWifiManager",
                               "DataMode", StringValue("HtMcs7"),
                               "ControlMode", StringValue("HtMcs7")); 

  WifiMacHelper mac;
  Ssid          ssid("ns3-wifi");

  mac.SetType("ns3::StaWifiMac",
              "Ssid", SsidValue(ssid),
              "QosSupported", BooleanValue(true),
              "ActiveProbing", BooleanValue(false));
  NetDeviceContainer staDevs = wifi.Install(phy, mac, sta);

  mac.SetType("ns3::ApWifiMac",
              "Ssid", SsidValue(ssid),
              "QosSupported", BooleanValue(true));
  NetDeviceContainer apDev = wifi.Install(phy, mac, ap);

  // ============================================================================
  // [REMOVED] Manual SetCwMin to allow standard EDCA values to be observed
  // ============================================================================

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
  Ipv4InterfaceContainer staIf = ip.Assign(staDevs);

  // Backoff & CW traces
  const char *acName[] = {"BE_Txop", "BK_Txop", "VI_Txop", "VO_Txop"};
  for (int i = 0; i < 4; ++i)
  {
    std::string pathB =
        "/NodeList/*/DeviceList/*/$ns3::WifiNetDevice/Mac/" + std::string(acName[i]) + "/BackoffTrace";
    std::string pathC =
        "/NodeList/*/DeviceList/*/$ns3::WifiNetDevice/Mac/" + std::string(acName[i]) + "/CwTrace";
    Config::Connect(pathB, MakeCallback(&BackoffTraceCb));
    Config::Connect(pathC, MakeCallback(&CwTraceCb));
  }

  // Enqueue 監聽 (Enabled Fix)
  Config::Connect("/NodeList/*/DeviceList/*/$ns3::WifiNetDevice/Mac/*/Queue/Enqueue",
                 MakeCallback(&EnqueueCb));
  
  // App Tx 監聽 (Manual Connection in Loop)
  // Config::Connect("/NodeList/*/ApplicationList/*/$ns3::OnOffApplication/Tx",
  //                MakeCallback(&AppTxCb));

  Config::Connect("/NodeList/*/DeviceList/*/$ns3::WifiNetDevice/Phy/PhyTxBegin",
                  MakeCallback(&PhyTxBeginCb));

  Config::Connect("/NodeList/0/DeviceList/*/$ns3::WifiNetDevice/Phy/MonitorSnifferRx",
                  MakeCallback(&MonitorSnifferRxCb));

  // Queue Size
  Config::SetDefault("ns3::WifiMacQueue::MaxSize", StringValue("10000p"));
  
  // UDP App
  uint16_t port = 5000;
  
  // AP as Sink
  PacketSinkHelper sink("ns3::UdpSocketFactory", InetSocketAddress(Ipv4Address::GetAny(), port));
  ApplicationContainer sinkApps = sink.Install(ap.Get(0));
  sinkApps.Start(Seconds(0.0));
  sinkApps.Stop(Seconds(simTime + 1.0));

  // Sources: Multi-AC per STA (BE + BK + VI + VO)
  OnOffHelper onoff("ns3::UdpSocketFactory", InetSocketAddress(apIf.GetAddress(0), port));
  onoff.SetAttribute("PacketSize", UintegerValue(pktSize));
  onoff.SetAttribute("DataRate", DataRateValue(DataRate(dataRate))); // Lower rate to avoid instant saturation
  onoff.SetAttribute("OnTime", StringValue("ns3::ConstantRandomVariable[Constant=1]"));
  onoff.SetAttribute("OffTime", StringValue("ns3::ConstantRandomVariable[Constant=0]"));
  
  Ptr<UniformRandomVariable> var = CreateObject<UniformRandomVariable>();
  
  ApplicationContainer srcApps;
  for (uint32_t i = 0; i < nSta; ++i)
  {
      // 1. BE Flow (Low Priority)
      onoff.SetAttribute("Tos", UintegerValue(0x00)); // AC_BE
      ApplicationContainer app1 = onoff.Install(sta.Get(i));
      app1.Start(Seconds(var->GetValue(0.0, 1)));
      app1.Stop(Seconds(simTime));
      app1.Get(0)->TraceConnect("Tx", "/ApplicationList/" + std::to_string(4*i + 0), MakeCallback(&AppTxCb));
      srcApps.Add(app1);

      // 2. BK Flow (Background)
      onoff.SetAttribute("Tos", UintegerValue(0x20)); // AC_BK
      ApplicationContainer app2 = onoff.Install(sta.Get(i));
      app2.Start(Seconds(var->GetValue(0.0, 1)));
      app2.Stop(Seconds(simTime));
      app2.Get(0)->TraceConnect("Tx", "/ApplicationList/" + std::to_string(4*i + 1), MakeCallback(&AppTxCb));
      srcApps.Add(app2);

      // 3. VI Flow (Video)
      onoff.SetAttribute("Tos", UintegerValue(0xa0)); // AC_VI
      ApplicationContainer app3 = onoff.Install(sta.Get(i));
      app3.Start(Seconds(var->GetValue(0.0, 1)));
      app3.Stop(Seconds(simTime));
      app3.Get(0)->TraceConnect("Tx", "/ApplicationList/" + std::to_string(4*i + 2), MakeCallback(&AppTxCb));
      srcApps.Add(app3);

      // 4. VO Flow (Voice)
      onoff.SetAttribute("Tos", UintegerValue(0xc0)); // AC_VO
      ApplicationContainer app4 = onoff.Install(sta.Get(i));
      app4.Start(Seconds(var->GetValue(0.0, 1)));
      app4.Stop(Seconds(simTime));
      app4.Get(0)->TraceConnect("Tx", "/ApplicationList/" + std::to_string(4*i + 3), MakeCallback(&AppTxCb));
      srcApps.Add(app4);
  }

  // Run
  Simulator::Stop(Seconds(simTime));
  Simulator::Run();

  // ====== 統計輸出 (Per AC) ======
  double duration   = simTime - g_warmupTime; 
  
  std::cout << "\n=== RESULTS (APP) ===\n";
  std::cout << "STAs: " << nSta << " (Each running BE+BK+VI+VO)\n";
  
  std::cout << "\n=== RESULTS (MAC by AC) ===\n";
  const char *acLabel[] = {"BE", "BK", "VI", "VO"};
  
  for (int i = 0; i < 4; ++i) {
      double throughput = duration > 0 ? (g_stats[i].rxBytes * 8.0) / duration / 1e6 : 0.0;
      double avgTx = g_stats[i].txUnique > 0 ? (double)g_stats[i].txMpdu / g_stats[i].txUnique : 0.0;
      
      // LOSS CALCULATION FIX: (AppTx - Rx) / AppTx
      uint64_t txApp = g_stats[i].txApp;
      uint64_t rx = g_stats[i].rxUnique;
      double loss = txApp > 0 ? (double)(txApp > rx ? txApp - rx : 0) / txApp * 100.0 : 0.0;

      double aDelay = g_stats[i].countAirDelay ? g_stats[i].sumAirDelay / g_stats[i].countAirDelay : 0.0;
      double eDelay = g_stats[i].countE2EDelay ? g_stats[i].sumE2EDelay / g_stats[i].countE2EDelay : 0.0;

      std::cout << "--- AC_" << acLabel[i] << " ---\n";
      std::cout << "  App Tx Packets:   " << txApp << "\n";
      std::cout << "  Observed Min CW:  " << g_stats[i].minCw << "\n";
      std::cout << "  Observed Max CW:  " << g_stats[i].maxCw << "\n";
      std::cout << "  Throughput:       " << throughput << " Mbps\n";
      std::cout << "  Packet Loss:      " << loss << " % (App Layer)\n";
      std::cout << "  Avg Retries/Pkt:  " << (avgTx > 1.0 ? avgTx - 1.0 : 0.0) << "\n";
      std::cout << "  Avg Air Delay:    " << aDelay << " us\n";
      std::cout << "  Avg E2E Delay:    " << eDelay << " us (Enqueue -> Rx)\n";
  }

  Simulator::Destroy();
  return 0;
}