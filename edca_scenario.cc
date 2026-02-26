#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/internet-module.h"
#include "ns3/wifi-module.h"
#include "ns3/mobility-module.h"
#include "ns3/applications-module.h"

#include <array>
#include <iostream>
#include <sstream>
#include <string>
#include <unordered_map>
#include <unordered_set>

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("EdcaScenario");

namespace {
constexpr uint8_t kAcBe = 0;
constexpr uint8_t kAcBk = 1;
constexpr uint8_t kAcVi = 2;
constexpr uint8_t kAcVo = 3;

struct DelayStats
{
  uint64_t count{0};
  double   sumUs{0.0};
};

std::unordered_map<uint64_t, Time>    g_enqueueTime;
std::unordered_map<uint64_t, uint8_t> g_acByUid;
std::unordered_set<uint64_t>          g_seenFirstTx;
std::array<DelayStats, 4>             g_queueDelay;

uint8_t
TosForAc(uint8_t ac)
{
  switch (ac)
  {
  case kAcBk: return 0x20;
  case kAcVi: return 0xa0;
  case kAcVo: return 0xc0;
  case kAcBe:
  default: return 0x00;
  }
}

const char *
AcName(uint8_t ac)
{
  switch (ac)
  {
  case kAcBe: return "BE";
  case kAcBk: return "BK";
  case kAcVi: return "VI";
  case kAcVo: return "VO";
  default: return "?";
  }
}

std::array<double, 4>
ParseFractions(const std::string &text)
{
  std::array<double, 4> fractions{0.3333, 0.3333, 0.1667, 0.1667};
  if (text.empty())
  {
    return fractions;
  }

  std::array<double, 4> parsed{};
  std::stringstream     ss(text);
  std::string           item;
  size_t                idx = 0;
  while (std::getline(ss, item, ',') && idx < parsed.size())
  {
    std::stringstream is(item);
    double            value = 0.0;
    is >> value;
    parsed[idx++] = value;
  }
  if (idx != parsed.size())
  {
    std::cerr << "[WARN] Invalid fraction count. Use default fractions.\n";
    return fractions;
  }

  double sum = 0.0;
  for (double v : parsed)
  {
    sum += v;
  }
  if (sum <= 0.0)
  {
    std::cerr << "[WARN] Invalid fraction sum. Use default fractions.\n";
    return fractions;
  }

  for (double &v : parsed)
  {
    v /= sum;
  }
  return parsed;
}

void
EnqueueCb(std::string ctx, Ptr<const WifiMpdu> mpdu, uint8_t ac)
{
  if (!mpdu)
  {
    return;
  }
  Ptr<const Packet> p = mpdu->GetPacket();
  if (!p)
  {
    return;
  }

  uint64_t uid = p->GetUid();
  g_enqueueTime[uid] = Simulator::Now();
  g_acByUid[uid]     = ac;
  g_seenFirstTx.erase(uid);
}

void
PhyTxBeginCb(std::string ctx, Ptr<const Packet> p, double /*txPowerW*/)
{
  WifiMacHeader hdr;
  Ptr<Packet>   copy = p->Copy();
  if (!copy->PeekHeader(hdr))
  {
    return;
  }
  if (!hdr.IsData())
  {
    return;
  }
  if (hdr.GetAddr1().IsBroadcast())
  {
    return;
  }

  uint64_t uid = p->GetUid();
  auto     itAc = g_acByUid.find(uid);
  if (itAc == g_acByUid.end())
  {
    return;
  }
  if (!g_seenFirstTx.insert(uid).second)
  {
    return;
  }

  auto itEnq = g_enqueueTime.find(uid);
  if (itEnq == g_enqueueTime.end())
  {
    return;
  }

  double delayUs = (Simulator::Now() - itEnq->second).GetMicroSeconds();
  g_queueDelay[itAc->second].sumUs += delayUs;
  g_queueDelay[itAc->second].count++;
}
} // namespace

int
main(int argc, char *argv[])
{
  uint32_t    nSta        = 10;
  double      simTime     = 10.0;
  uint32_t    pktSize     = 1200;
  std::string totalRate   = "60Mbps";
  std::string fractionStr = "0.3333,0.3333,0.1667,0.1667";

  CommandLine cmd(__FILE__);
  cmd.AddValue("nSta", "Number of STAs", nSta);
  cmd.AddValue("sim", "Simulation time (s)", simTime);
  cmd.AddValue("pkt", "Packet size (bytes)", pktSize);
  cmd.AddValue("rate", "Total offered rate per STA", totalRate);
  cmd.AddValue("fractions", "Traffic fractions BE,BK,VI,VO", fractionStr);
  cmd.Parse(argc, argv);

  auto fractions = ParseFractions(fractionStr);

  NodeContainer ap;
  NodeContainer sta;
  ap.Create(1);
  sta.Create(nSta);

  YansWifiChannelHelper chan = YansWifiChannelHelper::Default();
  YansWifiPhyHelper     phy;
  phy.SetChannel(chan.Create());

  WifiHelper wifi;
  wifi.SetStandard(WIFI_STANDARD_80211a);
  wifi.SetRemoteStationManager("ns3::ConstantRateWifiManager",
                               "DataMode", StringValue("OfdmRate24Mbps"),
                               "ControlMode", StringValue("OfdmRate24Mbps"));

  WifiMacHelper mac;
  Ssid          ssid("edca-scenario");

  mac.SetType("ns3::StaWifiMac",
              "Ssid", SsidValue(ssid),
              "QosSupported", BooleanValue(true),
              "ActiveProbing", BooleanValue(false));
  NetDeviceContainer staDevs = wifi.Install(phy, mac, sta);

  mac.SetType("ns3::ApWifiMac",
              "Ssid", SsidValue(ssid),
              "QosSupported", BooleanValue(true));
  NetDeviceContainer apDev = wifi.Install(phy, mac, ap);

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

  InternetStackHelper stack;
  stack.Install(ap);
  stack.Install(sta);

  Ipv4AddressHelper ip;
  ip.SetBase("10.1.1.0", "255.255.255.0");
  Ipv4InterfaceContainer apIf = ip.Assign(apDev);
  ip.Assign(staDevs);

  Config::SetDefault("ns3::WifiMacQueue::MaxSize", StringValue("10000p"));

  const std::array<std::string, 4> queueNames = {
      "BE_Txop",
      "BK_Txop",
      "VI_Txop",
      "VO_Txop",
  };

  for (size_t ac = 0; ac < queueNames.size(); ++ac)
  {
    std::string path =
        "/NodeList/*/DeviceList/*/$ns3::WifiNetDevice/Mac/" + queueNames[ac] + "/Queue/Enqueue";
    Config::Connect(path, MakeBoundCallback(&EnqueueCb, static_cast<uint8_t>(ac)));
  }

  Config::Connect("/NodeList/*/DeviceList/*/$ns3::WifiNetDevice/Phy/PhyTxBegin",
                  MakeCallback(&PhyTxBeginCb));

  uint16_t port = 5000;
  PacketSinkHelper sink("ns3::UdpSocketFactory", InetSocketAddress(Ipv4Address::GetAny(), port));
  ApplicationContainer sinkApps = sink.Install(ap.Get(0));
  sinkApps.Start(Seconds(0.0));
  sinkApps.Stop(Seconds(simTime + 1.0));

  OnOffHelper onoff("ns3::UdpSocketFactory", InetSocketAddress(apIf.GetAddress(0), port));
  onoff.SetAttribute("PacketSize", UintegerValue(pktSize));
  onoff.SetAttribute("OnTime", StringValue("ns3::ConstantRandomVariable[Constant=1]"));
  onoff.SetAttribute("OffTime", StringValue("ns3::ConstantRandomVariable[Constant=0]"));

  DataRate total(totalRate);
  uint64_t totalBps = total.GetBitRate();

  Ptr<UniformRandomVariable> startVar = CreateObject<UniformRandomVariable>();
  for (uint32_t i = 0; i < nSta; ++i)
  {
    for (uint8_t ac = 0; ac < 4; ++ac)
    {
      if (fractions[ac] <= 0.0)
      {
        continue;
      }
      uint64_t acRate = static_cast<uint64_t>(totalBps * fractions[ac]);
      if (acRate == 0)
      {
        continue;
      }

      onoff.SetAttribute("DataRate", DataRateValue(DataRate(acRate)));
      onoff.SetAttribute("Tos", UintegerValue(TosForAc(ac)));

      ApplicationContainer app = onoff.Install(sta.Get(i));
      app.Start(Seconds(startVar->GetValue(0.0, 0.1)));
      app.Stop(Seconds(simTime));
    }
  }

  Simulator::Stop(Seconds(simTime));
  Simulator::Run();

  std::cout << "\n=== EDCA Scenario Results ===\n";
  std::cout << "STAs: " << nSta << " , SimTime: " << simTime << " s\n";
  std::cout << "Traffic fractions (BE,BK,VI,VO): " << fractions[0] << ", " << fractions[1]
            << ", " << fractions[2] << ", " << fractions[3] << "\n";
  std::cout << "Total rate per STA: " << totalRate << "\n";

  for (uint8_t ac = 0; ac < 4; ++ac)
  {
    double avgDelayUs = g_queueDelay[ac].count
                            ? (g_queueDelay[ac].sumUs / g_queueDelay[ac].count)
                            : 0.0;
    std::cout << "Avg MAC Queue Delay " << AcName(ac) << ": " << avgDelayUs << " us\n";
  }

  Simulator::Destroy();
  return 0;
}
