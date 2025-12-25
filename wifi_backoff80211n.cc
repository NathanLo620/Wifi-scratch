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
  uint64_t txMpdu{0};
  uint64_t txUnique{0};
  uint64_t rxUnique{0};
  uint64_t rxBytes{0};
  double   sumQueueDelay{0.0};
  uint64_t countQueueDelay{0};
  double   sumAirDelay{0.0};
  uint64_t countAirDelay{0};
  
  uint32_t minCw{99999}; // Track observed Min CW
  uint32_t maxCw{0};
};

static std::unordered_map<uint32_t, uint32_t> g_lastCwByNode;

// 0=BE, 1=BK, 2=VI, 3=VO
static std::vector<AcStats> g_stats(4);
static double g_warmupTime = 0.5;

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

static std::map<TxKey, uint32_t> g_txCountBySeq;  // Key: (SA, Seq, TID)
static std::set<TxKey>           g_seenFirstTx;   // Key: (SA, Seq, TID)
static std::map<TxKey, Time>     g_enqTimeBySeq;  // Key: (SA, Seq, TID)
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
EnqueueCb(std::string context, Ptr<const WifiMpdu> mpdu)
{
  WifiMacHeader hdr;
  if (mpdu->GetHeader().IsData()) {
      hdr = mpdu->GetHeader();
      TxKey key;
      key.sa = MacToString(hdr.GetAddr2());
      key.seq = hdr.GetSequenceNumber();
      key.tid = 0;
      if (hdr.IsQosData()) key.tid = hdr.GetQosTid();
      
      uint8_t ac = 0;
      if (hdr.IsQosData()) {
          ac = TidToAc(hdr.GetQosTid());
      }
      
      g_enqTimeBySeq[key] = Simulator::Now();
      g_keyToAc[key] = ac;
      g_seenFirstTx.erase(key);
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

  g_stats[ac].txMpdu++;
  
  auto &cnt = g_txCountBySeq[key];
  ++cnt;
  
  if (!g_seenFirstTx.count(key))
  {
    g_seenFirstTx.insert(key);
    g_stats[ac].txUnique++;
  }

  // Calculate Queue Delay (only for the first transmission attempt of this UID)
  auto itEnq = g_enqTimeBySeq.find(key);
  if (itEnq != g_enqTimeBySeq.end() && cnt == 1u)
  {
    double dUs = (Simulator::Now() - itEnq->second).GetMicroSeconds();
    g_stats[ac].sumQueueDelay += dUs;
    g_stats[ac].countQueueDelay++;
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
PhyRxEndOkCb(std::string ctx, Ptr<const Packet> p)
{
  WifiMacHeader hdr;
  Ptr<Packet>   cp = p->Copy();
  if (!cp->PeekHeader(hdr)) return;
  if (!hdr.IsData()) return;

  uint32_t nid = ExtractNodeId(ctx);
  if (nid != 0) return; // Only process RX at AP (node 0)

  if (Simulator::Now().GetSeconds() < g_warmupTime) return;

  // Identify AC
  uint8_t ac = 0;
  if (hdr.IsQosData()) ac = TidToAc(hdr.GetQosTid());
  
  g_stats[ac].rxBytes += p->GetSize();

  AirKey akey;
  akey.sa  = MacToString(hdr.GetAddr2());
  akey.seq = hdr.GetSequenceNumber();
  akey.tid = 0;
  if (hdr.IsQosData()) akey.tid = hdr.GetQosTid();

  auto it = g_airTxTime.find(akey);
  if (it == g_airTxTime.end()) return; 

  double dUs = (Simulator::Now() - it->second).GetMicroSeconds();
  g_stats[ac].sumAirDelay += dUs;
  g_stats[ac].countAirDelay++;
  
  g_airRxUnique.insert(akey);
  g_stats[ac].rxUnique++;
  g_airTxTime.erase(it);
}

// ---------------------- Main ----------------------
int
main(int argc, char *argv[])
{
  uint32_t    nSta    = 20;
  double      simTime = 10.0;
  uint32_t    pktSize = 1200;
  bool        enableRts = false;

  CommandLine cmd(__FILE__);
  cmd.AddValue("nSta", "Number of STAs", nSta);
  cmd.AddValue("sim", "Simulation time", simTime);
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
                           "Rho", StringValue("ns3::UniformRandomVariable[Min=1.0|Max=3.0]"));
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

  // Enqueue 監聽
  for (int i = 0; i < 4; ++i) {
      Config::Connect("/NodeList/*/DeviceList/*/$ns3::WifiNetDevice/Mac/" + std::string(acName[i]) + "/Queue/Enqueue",
                      MakeCallback(&EnqueueCb));
  }

  Config::Connect("/NodeList/*/DeviceList/*/$ns3::WifiNetDevice/Phy/PhyTxBegin",
                  MakeCallback(&PhyTxBeginCb));

  Config::Connect("/NodeList/*/DeviceList/*/$ns3::WifiNetDevice/Phy/PhyRxEnd",
                  MakeCallback(&PhyRxEndOkCb));

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
  onoff.SetAttribute("DataRate", DataRateValue(DataRate("10Mbps"))); // Lower rate to avoid instant saturation
  onoff.SetAttribute("OnTime", StringValue("ns3::ConstantRandomVariable[Constant=1]"));
  onoff.SetAttribute("OffTime", StringValue("ns3::ConstantRandomVariable[Constant=0]"));
  
  Ptr<UniformRandomVariable> var = CreateObject<UniformRandomVariable>();
  
  ApplicationContainer srcApps;
  for (uint32_t i = 0; i < nSta; ++i)
  {
      // 1. BE Flow (Low Priority)
      onoff.SetAttribute("Tos", UintegerValue(0x00)); // AC_BE
      ApplicationContainer app1 = onoff.Install(sta.Get(i));
      app1.Start(Seconds(var->GetValue(0.0, 0.1)));
      app1.Stop(Seconds(simTime));
      srcApps.Add(app1);

      // 2. BK Flow (Background)
      onoff.SetAttribute("Tos", UintegerValue(0x20)); // AC_BK
      ApplicationContainer app2 = onoff.Install(sta.Get(i));
      app2.Start(Seconds(var->GetValue(0.0, 0.1)));
      app2.Stop(Seconds(simTime));
      srcApps.Add(app2);

      // 3. VI Flow (Video)
      onoff.SetAttribute("Tos", UintegerValue(0xa0)); // AC_VI
      ApplicationContainer app3 = onoff.Install(sta.Get(i));
      app3.Start(Seconds(var->GetValue(0.0, 0.1)));
      app3.Stop(Seconds(simTime));
      srcApps.Add(app3);

      // 4. VO Flow (Voice)
      onoff.SetAttribute("Tos", UintegerValue(0xc0)); // AC_VO
      ApplicationContainer app4 = onoff.Install(sta.Get(i));
      app4.Start(Seconds(var->GetValue(0.0, 0.1)));
      app4.Stop(Seconds(simTime));
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
      double loss = g_stats[i].txUnique > 0 ? 
          (double)(g_stats[i].txUnique > g_stats[i].rxUnique ? g_stats[i].txUnique - g_stats[i].rxUnique : 0) / g_stats[i].txUnique * 100.0 : 0.0;
      double qDelay = g_stats[i].countQueueDelay ? g_stats[i].sumQueueDelay / g_stats[i].countQueueDelay : 0.0;
      double aDelay = g_stats[i].countAirDelay ? g_stats[i].sumAirDelay / g_stats[i].countAirDelay : 0.0;

      std::cout << "--- AC_" << acLabel[i] << " ---\n";
      std::cout << "  Observed Min CW:  " << g_stats[i].minCw << "\n";
      std::cout << "  Observed Max CW:  " << g_stats[i].maxCw << "\n";
      std::cout << "  Throughput:       " << throughput << " Mbps\n";
      std::cout << "  Packet Loss:      " << loss << " %\n";
      std::cout << "  Avg Retries/Pkt:  " << (avgTx > 1.0 ? avgTx - 1.0 : 0.0) << "\n";
      std::cout << "  Avg Queue Delay:  " << qDelay << " us\n";
      std::cout << "  Avg Air Delay:    " << aDelay << " us\n";
  }

  Simulator::Destroy();
  return 0;
}