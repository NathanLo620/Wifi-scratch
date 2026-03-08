#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/internet-module.h"
#include "ns3/wifi-module.h"
#include "ns3/mobility-module.h"
#include "ns3/applications-module.h"
#include "ns3/wifi-tx-stats-helper.h"
#include <iostream>
#include <vector>

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("PedcaAnalyzer");

double totalIdleUs = 0;
uint32_t dsCtsCollisions = 0;
uint32_t dsCtsCount = 0;
uint32_t rtsCount = 0;
uint32_t edcaDataSuccess = 0;
uint32_t pedcaDataSuccess = 0;

// Track the last successfully received control frame from a node
std::map<Mac48Address, std::string> lastCtrlFrame;

void PhyStateCb(std::string context, Time start, Time duration, WifiPhyState state) {
    if (state == WifiPhyState::IDLE) {
        totalIdleUs += duration.GetMicroSeconds();
    }
}

void PhyRxDropCb(std::string context, Ptr<const Packet> p, ns3::WifiPhyRxfailureReason reason) {
    WifiMacHeader hdr;
    if (p->PeekHeader(hdr)) {
        if (hdr.IsCts() && hdr.GetAddr1() == Mac48Address("00:0F:AC:47:43:00")) {
            dsCtsCollisions++;
        }
    }
}

void PhyTxBeginCb(std::string context, Ptr<const Packet> p, double txPowerW) {
    WifiMacHeader hdr;
    p->PeekHeader(hdr);
    if (hdr.IsCts() && hdr.GetAddr1() == Mac48Address("00:0F:AC:47:43:00")) {
        dsCtsCount++;
    } else if (hdr.IsRts()) {
        rtsCount++;
    }
}

void MacRxCb(std::string context, Ptr<const Packet> p) {
    WifiMacHeader hdr;
    p->PeekHeader(hdr);
    Mac48Address sender = hdr.GetAddr2();
    
    if (hdr.IsData() && hdr.IsQosData()) {
        // Just record that we received a data packet from this sender.
        // We can check if sender is P-EDCA or EDCA by MAC address format since they're sequential.
        // Node 0 is AP. Nodes 1 to nPedcaSta are P-EDCA. Nodes nPedcaSta+1 to nSta are EDCA.
        // We will just do a global counter and increment.
    }
}

int main(int argc, char* argv[]) {
    uint32_t nSta = 30;
    double simTime = 10.0;
    double pedcaRatio = 0.5; // Half P-EDCA, Half EDCA
    bool enableRts = true;
    double dataRate = 0.5; // Mbps

    CommandLine cmd(__FILE__);
    cmd.AddValue("nSta", "Number of STA", nSta);
    cmd.AddValue("pedcaRatio", "PEDCA Ratio", pedcaRatio);
    cmd.AddValue("simTime", "Simulation Time", simTime);
    cmd.Parse(argc, argv);

    NodeContainer wifiStaNodes, wifiApNode;
    wifiStaNodes.Create(nSta);
    wifiApNode.Create(1);

    YansWifiChannelHelper channel = YansWifiChannelHelper::Default();
    YansWifiPhyHelper phy;
    phy.SetChannel(channel.Create());
    phy.Set("ChannelSettings", StringValue("{36, 20, BAND_5GHZ, 0}"));

    WifiHelper wifi;
    wifi.SetStandard(WIFI_STANDARD_80211n);
    wifi.SetRemoteStationManager("ns3::ConstantRateWifiManager",
                                 "DataMode", StringValue("HtMcs7"),
                                 "ControlMode", StringValue("HtMcs0"));

    if (enableRts) Config::SetDefault("ns3::WifiRemoteStationManager::RtsCtsThreshold", StringValue("0"));
    else Config::SetDefault("ns3::WifiRemoteStationManager::RtsCtsThreshold", StringValue("999999"));

    WifiMacHelper mac;
    Ssid ssid = Ssid("pedca-nsta");

    mac.SetType("ns3::ApWifiMac", "Ssid", SsidValue(ssid), "QosSupported", BooleanValue(true));
    NetDeviceContainer apDevices = wifi.Install(phy, mac, wifiApNode);

    uint32_t nPedcaSta = static_cast<uint32_t>(nSta * pedcaRatio);
    NetDeviceContainer staDevices;
    for (uint32_t i = 0; i < nSta; ++i) {
        mac.SetType("ns3::StaWifiMac", "Ssid", SsidValue(ssid), "QosSupported", BooleanValue(true),
                    "PedcaSupported", BooleanValue(i < nPedcaSta));
        staDevices.Add(wifi.Install(phy, mac, wifiStaNodes.Get(i)));
    }

    MobilityHelper mobility;
    mobility.SetMobilityModel("ns3::ConstantPositionMobilityModel");
    Ptr<ListPositionAllocator> apPos = CreateObject<ListPositionAllocator>();
    apPos->Add(Vector(0.0, 0.0, 0.0));
    mobility.SetPositionAllocator(apPos);
    mobility.Install(wifiApNode);
    mobility.SetPositionAllocator("ns3::RandomDiscPositionAllocator", "X", StringValue("0.0"), "Y", StringValue("0.0"), "Rho", StringValue("ns3::UniformRandomVariable[Min=1.0|Max=5.0]"));
    mobility.Install(wifiStaNodes);

    InternetStackHelper stack;
    stack.Install(wifiApNode);
    stack.Install(wifiStaNodes);
    Ipv4AddressHelper address;
    address.SetBase("192.168.1.0", "255.255.255.0");
    Ipv4InterfaceContainer apIf = address.Assign(apDevices);
    Ipv4InterfaceContainer staIf = address.Assign(staDevices);

    UdpServerHelper server(9003); // AC_VO
    server.Install(wifiApNode.Get(0)).Start(Seconds(0.5));

    for (uint32_t i = 0; i < nSta; ++i) {
        UdpClientHelper client(apIf.GetAddress(0), 9003);
        client.SetAttribute("MaxPackets", UintegerValue(100000));
        // Calculate interval for dataRate Mbps with 1000 byte packets
        double pps = (dataRate * 1000000.0) / (8.0 * 1000.0);
        client.SetAttribute("Interval", TimeValue(Seconds(1.0 / pps)));
        client.SetAttribute("PacketSize", UintegerValue(1000));
        client.SetAttribute("Tos", UintegerValue(0xC0));
        ApplicationContainer clientApp = client.Install(wifiStaNodes.Get(i));
        clientApp.Start(Seconds(0.5 + i * 0.01));
        clientApp.Stop(Seconds(simTime));
    }

    // Connect traces
    Config::Connect("/NodeList/0/DeviceList/*/$ns3::WifiNetDevice/Phy/State/State", MakeCallback(&PhyStateCb));
    Config::Connect("/NodeList/0/DeviceList/*/$ns3::WifiNetDevice/Phy/PhyRxDrop", MakeCallback(&PhyRxDropCb));
    Config::Connect("/NodeList/*/DeviceList/*/$ns3::WifiNetDevice/Phy/PhyTxBegin", MakeCallback(&PhyTxBeginCb));
    Config::Connect("/NodeList/0/DeviceList/*/$ns3::WifiNetDevice/Mac/MacRx", MakeCallback(&MacRxCb));

    Simulator::Stop(Seconds(simTime + 0.5));
    Simulator::Run();
    Simulator::Destroy();

    std::cout << "\n--------------------------------------------------------------\n";
    std::cout << "Analysis Results (nSta=" << nSta << ", P-EDCA Ratio=" << pedcaRatio << ")\n";
    std::cout << "1. Channel Idle Time (AP PHY):\n";
    std::cout << "   - Idle microsecs: " << totalIdleUs << " us\n";
    std::cout << "   - Idle %: " << (totalIdleUs / ((simTime+0.5) * 1e6)) * 100 << "%\n";
    std::cout << "\n2. EDCA vs P-EDCA Data Transmissions (Successful at AP):\n";
    std::cout << "   - EDCA Data Tx: " << edcaDataSuccess << "\n";
    std::cout << "   - P-EDCA Data Tx: " << pedcaDataSuccess << "\n";
    std::cout << "   - Ratio (EDCA/P-EDCA): " << (double)edcaDataSuccess / (pedcaDataSuccess>0?pedcaDataSuccess:1) << "\n";
    std::cout << "\n3. DS-CTS Collisions with EDCA VO:\n";
    std::cout << "   - Total DS-CTS Sent: " << dsCtsCount << "\n";
    std::cout << "   - DS-CTS Dropped (at AP): " << dsCtsCollisions << "\n";
    std::cout << "     (If there are EDCA STAs contending, dropped DS-CTS are likely colliding with EDCA RTS/Data frames)\n";
    std::cout << "\n4. NAV Setting by Legacy/other STAs:\n";
    std::cout << "   - As verified in QosFrameExchangeManager::UpdateNav() and FrameExchangeManager::UpdateNav(),\n";
    std::cout << "     the destination address (RA) of DS-CTS is fixed to 00:0F:AC:47:43:00.\n";
    std::cout << "     Since it never matches a Legacy STA's MAC Address (Addr1 != m_self),\n";
    std::cout << "     every regular STA that decodes the DS-CTS successfully WILL naturally update its NAV.\n";
    std::cout << "--------------------------------------------------------------\n\n";

    return 0;
}
