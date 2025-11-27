/* -*- Mode:C++; c-file-style:"gnu"; indent-tabs-mode:nil; -*- */
#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/internet-module.h"
#include "ns3/wifi-module.h"
#include "ns3/mobility-module.h"
#include "ns3/applications-module.h"
#include "ns3/yans-wifi-helper.h"
#include "ns3/seq-ts-header.h"
#include "ns3/wifi-mac-header.h"
#include "ns3/wifi-mpdu.h"
#include "ns3/timestamp-tag.h"
#include "ns3/mac48-address.h"
// [FIX] 加入 Txop 定義，這是公開 Header，可以安全引用
#include "ns3/txop.h"

#include <unordered_map>
#include <unordered_set>
#include <map>
#include <set>
#include <string>
#include <sstream>

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("WifiBackoffTrace");

static std::string
MacToString(const Mac48Address &addr)
{
  std::ostringstream oss;
  oss << addr;
  return oss.str();
}

// ---------------------- UDP Sender ----------------------
class UdpSeqTsSender : public Application
{
public:
  void Setup(Ipv4Address dst, uint16_t port, uint32_t pktSize, DataRate rate, uint8_t tos)
  {
    m_dst = InetSocketAddress(dst, port);
    m_pktSize = pktSize;
    m_rate = rate;
    m_tos = tos;
  }

private:
  Ptr<Socket> m_socket;
  Address     m_dst;
  uint32_t    m_pktSize{1200};
  DataRate    m_rate{"5Mbps"};
  uint8_t     m_tos{0};
  Time        m_next;
  uint32_t    m_seq{0};

  virtual void StartApplication()
  {
    m_socket = Socket::CreateSocket(GetNode(), UdpSocketFactory::GetTypeId());
    m_socket->Connect(m_dst);
    m_socket->SetIpTos(m_tos);
    m_next = Seconds((double)m_pktSize * 8 / m_rate.GetBitRate());
    Simulator::Schedule(Seconds(1.0), &UdpSeqTsSender::SendOne, this);
  }

  void SendOne()
  {
    Ptr<Packet> p = Create<Packet>(m_pktSize - SeqTsHeader().GetSerializedSize());
    SeqTsHeader h;
    h.SetSeq(++m_seq);
    p->AddHeader(h);

    TimestampTag ts;
    ts.SetTimestamp(Simulator::Now());
    p->AddPacketTag(ts);

    m_socket->Send(p);
    Simulator::Schedule(m_next, &UdpSeqTsSender::SendOne, this);
  }
};

// ---------------------- Global Stat Structs ----------------------
struct AppStats
{
  uint64_t rxBytes{0};
  uint64_t rxPkts{0};
  double   delaySumUs{0.0};
};
static AppStats g_app;

struct MacDelayStats
{
  uint64_t count{0};
  double   sumUs{0.0};
};
static MacDelayStats g_macQueueDelay;
static MacDelayStats g_macAirDelay;

// ---- MAC 封包 key ----
struct MacKey
{
  uint32_t nodeId;
  uint16_t seq;
  bool operator==(const MacKey &o) const
  {
    return nodeId == o.nodeId && seq == o.seq;
  }
};
struct MacKeyHash
{
  size_t operator()(const MacKey &k) const
  {
    return std::hash<uint32_t>()(k.nodeId) ^ (std::hash<uint16_t>()(k.seq) << 1);
  }
};

// ---- 空中路徑 key ----
struct AirKey
{
  std::string sa;
  uint16_t    seq;

  bool operator<(const AirKey &o) const
  {
    if (sa < o.sa) return true;
    if (sa > o.sa) return false;
    return seq < o.seq;
  }
};

// ---- 追蹤容器 ----
static std::unordered_map<uint32_t, uint32_t> g_lastCwByNode;
static std::unordered_map<MacKey, uint32_t, MacKeyHash> g_cwInitByPkt;
static std::unordered_map<MacKey, uint32_t, MacKeyHash> g_txCountByPkt;
static std::unordered_set<MacKey, MacKeyHash>           g_seenFirstTx;
static std::unordered_map<MacKey, Time, MacKeyHash>     g_enqTimeByPkt;

static std::map<AirKey, Time> g_airTxTime;
static std::set<AirKey> g_airRxUnique;

static uint64_t g_macTxMpdu   = 0;
static uint64_t g_macTxUnique = 0;

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

// [[maybe_unused]] 避免編譯器警告
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
ApRxCallback(Ptr<Socket> sock)
{
  Address from;
  while (Ptr<Packet> p = sock->RecvFrom(from))
  {
    g_app.rxBytes += p->GetSize();
    g_app.rxPkts++;

    TimestampTag ts;
    if (p->PeekPacketTag(ts))
    {
      g_app.delaySumUs += (Simulator::Now() - ts.GetTimestamp()).GetMicroSeconds();
    }
  }
}

static void
BackoffTraceCb(uint32_t backoff, uint8_t ac)
{
  
  std::cout << Simulator::Now().GetSeconds()
            << "s [TRACE] BackoffSlots=" << backoff
            << " CW=" << g_lastCwByNode[0]
            << " AC=" << (int)ac << "(" << AcName(ac) << ")\n";
  
}

static void
CwTraceCb(std::string ctx, uint32_t cw, uint8_t ac)
{
  uint32_t nid = ExtractNodeId(ctx);
  g_lastCwByNode[nid] = cw;
}

static void
EnqueueCb(std::string ctx, Ptr<const WifiMpdu> mpdu)
{
  WifiMacHeader hdr = mpdu->GetHeader();
  if (!hdr.IsData()) return;

  uint32_t nid = ExtractNodeId(ctx);
  MacKey   key{nid, hdr.GetSequenceNumber()};

  if (!g_cwInitByPkt.count(key))
  {
    uint32_t cw0 = 0;
    auto     it  = g_lastCwByNode.find(nid);
    if (it != g_lastCwByNode.end()) cw0 = it->second;
    g_cwInitByPkt[key] = cw0;
  }

  g_enqTimeByPkt[key] = Simulator::Now();
  g_seenFirstTx.erase(key);
}

static void
PhyTxBeginCb(std::string ctx, Ptr<const Packet> p, double /*txPowerW*/)
{
  WifiMacHeader hdr;
  Ptr<Packet>   cp = p->Copy();
  if (!cp->PeekHeader(hdr)) return;
  if (!hdr.IsData()) return;

  uint32_t nid = ExtractNodeId(ctx);
  MacKey   key{nid, hdr.GetSequenceNumber()};

  ++g_macTxMpdu;
  auto &cnt = g_txCountByPkt[key];
  ++cnt;
  if (!g_seenFirstTx.count(key))
  {
    g_seenFirstTx.insert(key);
    ++g_macTxUnique;
  }

  auto itEnq = g_enqTimeByPkt.find(key);
  if (itEnq != g_enqTimeByPkt.end() && cnt == 1u)
  {
    double dUs = (Simulator::Now() - itEnq->second).GetMicroSeconds();
    g_macQueueDelay.sumUs += dUs;
    g_macQueueDelay.count++;
  }

  if (nid != 0)
  {
    AirKey akey;
    akey.sa  = MacToString(hdr.GetAddr2());
    akey.seq = hdr.GetSequenceNumber();
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
  if (nid != 0) return;

  AirKey akey;
  akey.sa  = MacToString(hdr.GetAddr2());
  akey.seq = hdr.GetSequenceNumber();

  auto it = g_airTxTime.find(akey);
  if (it == g_airTxTime.end()) return;

  double dUs = (Simulator::Now() - it->second).GetMicroSeconds();
  g_macAirDelay.sumUs += dUs;
  g_macAirDelay.count++;
  g_airRxUnique.insert(akey);
  g_airTxTime.erase(it);
}

// ---------------------- Main ----------------------
int
main(int argc, char *argv[])
{
  uint32_t    nSta    = 5;
  double      simTime = 10.0;
  std::string rateStr = "2Mbps";
  uint32_t    pktSize = 1200;
  uint8_t     ac      = 0;
  uint32_t    cwMin   = 7;

  CommandLine cmd(__FILE__);
  cmd.AddValue("nSta", "Number of STAs", nSta);
  cmd.AddValue("sim", "Simulation time", simTime);
  cmd.AddValue("rate", "UDP sending rate", rateStr);
  cmd.AddValue("pkt", "Packet size (bytes)", pktSize);
  cmd.AddValue("ac", "Access Category (0=BE,1=BK,2=VI,3=VO)", ac);
  cmd.AddValue("cwMin", "Initial Contention Window size", cwMin);
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
  wifi.SetStandard(WIFI_STANDARD_80211a);
  wifi.SetRemoteStationManager("ns3::MinstrelWifiManager");

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
  // [FIX] 透過 C++ 介面直接設定 SetMinCw，繞過 Attribute 系統的混亂
  // ============================================================================
  
  // 1. 決定目標 AC 的 Txop 屬性名稱
  std::string queueName = "BE_Txop"; 
  switch (ac) {
      case 0: queueName = "BE_Txop"; break;
      case 1: queueName = "BK_Txop"; break;
      case 2: queueName = "VI_Txop"; break;
      case 3: queueName = "VO_Txop"; break;
      default: queueName = "BE_Txop"; break;
  }

  // 2. 定義設定函式
  auto SetCwMinForNodes = [&](NodeContainer &nodes) {
    for (uint32_t i = 0; i < nodes.GetN(); ++i) {
        Ptr<Node> node = nodes.Get(i);
        Ptr<WifiNetDevice> dev = DynamicCast<WifiNetDevice>(node->GetDevice(0));
        if (!dev) continue;

        Ptr<WifiMac> wifiMac = dev->GetMac();
        if (!wifiMac) continue;

        // 獲取屬性中的指標 (Ptr<Object>)
        PointerValue ptr;
        bool found = wifiMac->GetAttributeFailSafe(queueName, ptr);
        if (!found) continue;
        
        Ptr<Object> txopObj = ptr.Get<Object>();
        if (!txopObj) continue;

        // [CRITICAL] 將 Object 轉型為 Txop (需要 include ns3/txop.h)
        Ptr<Txop> txop = DynamicCast<Txop>(txopObj);
        if (txop) {
            // 直接呼叫 C++ 函式，這是最安全的方法
            // 即使 MinCw 屬性被廢棄，這個 SetMinCw 函式依然存在並負責處理內部邏輯
            txop->SetMinCw(cwMin);
        }
    }
  };

  // 3. 執行設定
  SetCwMinForNodes(sta);
  SetCwMinForNodes(ap);

  std::cout << "[INFO] Manually set " << queueName << " MinCw to " << cwMin << " via C++ method.\n";
  // ============================================================================

  // Mobility
  MobilityHelper mob;
  mob.SetMobilityModel("ns3::ConstantPositionMobilityModel");
  mob.Install(ap);
  for (uint32_t i = 0; i < nSta; ++i)
  {
    mob.Install(sta.Get(i));
  }

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
    Config::ConnectWithoutContext(pathB, MakeCallback(&BackoffTraceCb));
    Config::Connect(pathC, MakeCallback(&CwTraceCb));
  }

  // Enqueue 監聽
  Config::Connect("/NodeList/*/DeviceList/*/$ns3::WifiNetDevice/Mac/" + queueName + "/Queue/Enqueue",
                  MakeCallback(&EnqueueCb));

  Config::Connect("/NodeList/*/DeviceList/*/$ns3::WifiNetDevice/Phy/PhyTxBegin",
                  MakeCallback(&PhyTxBeginCb));

  Config::Connect("/NodeList/*/DeviceList/*/$ns3::WifiNetDevice/Phy/PhyRxEnd",
                  MakeCallback(&PhyRxEndOkCb));

  // UDP App
  uint16_t   port = 3000;
  Ptr<Socket> rxSock =
      Socket::CreateSocket(ap.Get(0), UdpSocketFactory::GetTypeId());
  rxSock->Bind(InetSocketAddress(Ipv4Address::GetAny(), port));
  rxSock->SetRecvCallback(MakeCallback(&ApRxCallback));

  // Map AC to ToS (DSCP)
  uint8_t tos = 0x00; // AC_BE
  switch (ac)
  {
  case 1: tos = 0x20; break; // AC_BK
  case 2: tos = 0xa0; break; // AC_VI
  case 3: tos = 0xc0; break; // AC_VO
  default: tos = 0x00; break; // AC_BE
  }

  for (uint32_t i = 0; i < nSta; ++i)
  {
    Ptr<UdpSeqTsSender> app = CreateObject<UdpSeqTsSender>();
    app->Setup(apIf.GetAddress(0), port, pktSize, DataRate(rateStr), tos);
    sta.Get(i)->AddApplication(app);
    app->SetStartTime(Seconds(0.0));
    app->SetStopTime(Seconds(simTime));
  }

  // Run
  Simulator::Stop(Seconds(simTime));
  Simulator::Run();

  // ====== 統計輸出 ======
  double duration   = simTime - 1.0; 
  double throughput = duration > 0 ? (g_app.rxBytes * 8.0) / duration / 1e6 : 0.0;
  double avgAppDelayUs =
      g_app.rxPkts ? (g_app.delaySumUs / g_app.rxPkts) : 0.0;

  double avgMacQueueDelayUs =
      g_macQueueDelay.count ? (g_macQueueDelay.sumUs / g_macQueueDelay.count) : 0.0;
  double avgMacAirDelayUs =
      g_macAirDelay.count ? (g_macAirDelay.sumUs / g_macAirDelay.count) : 0.0;

  uint64_t macRxUnique = g_airRxUnique.size();

  double avgTxPerPkt = 0.0;
  double avgRetryPerPkt = 0.0;
  if (g_macTxUnique > 0)
  {
    uint64_t sumTx = 0;
    for (auto &kv : g_txCountByPkt)
    {
      sumTx += kv.second;
    }
    avgTxPerPkt     = static_cast<double>(sumTx) / static_cast<double>(g_macTxUnique);
    avgRetryPerPkt  = avgTxPerPkt - 1.0;
  }

  double macLossRate = 0.0;
  if (g_macTxUnique > 0)
  {
    uint64_t lost = (g_macTxUnique > macRxUnique) ? (g_macTxUnique - macRxUnique) : 0;
    macLossRate    = static_cast<double>(lost) / static_cast<double>(g_macTxUnique) * 100.0;
  }

  std::cout << "\n=== RESULTS (APP) ===\n";
  std::cout << "STAs: " << nSta << " , SimTime: " << simTime << " s\n";
  std::cout << "CWmin Setting: " << cwMin << " (for " << queueName << ")\n";
  std::cout << "RxPkts=" << g_app.rxPkts
            << " , Throughput=" << throughput << " Mbps\n";
  std::cout << "Avg One-way Delay (UDP) = " << avgAppDelayUs << " us\n";

  std::cout << "\n=== RESULTS (MAC) ===\n";
  std::cout << "TX MPDUs (incl. retries): " << g_macTxMpdu << "\n";
  std::cout << "TX unique MAC packets:    " << g_macTxUnique << "\n";
  std::cout << "RX unique (SA,Seq):       " << macRxUnique << "\n";
  std::cout << "MAC Packet Loss Rate:     " << macLossRate << " %\n";
  std::cout << "Avg MAC Queue Delay (Enq->1stTx): "
            << avgMacQueueDelayUs << " us\n";
  std::cout << "Avg MAC Air Delay (TxBegin->RxEndOk): "
            << avgMacAirDelayUs << " us\n";
  std::cout << "Avg transmissions/pkt:    " << avgTxPerPkt << "\n";
  std::cout << "Avg retransmissions/pkt:  " << avgRetryPerPkt << "\n";

  uint32_t show = 10, cnt = 0;
  std::cout << "\nSample per-packet (CW0, retries):\n";
  for (auto &kv : g_txCountByPkt)
  {
    const MacKey &k   = kv.first;
    uint32_t      txn = kv.second;
    uint32_t      cw0 = g_cwInitByPkt.count(k) ? g_cwInitByPkt[k] : 0;
    std::cout << "  Node " << k.nodeId << " Seq " << k.seq
              << "  CW0=" << cw0
              << "  retries=" << (txn > 1 ? (txn - 1) : 0) << "\n";
    if (++cnt >= show) break;
  }

  Simulator::Destroy();
  return 0;
}