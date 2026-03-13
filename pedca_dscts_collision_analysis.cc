/*
 * P-EDCA DS-CTS Collision & State Analysis
 * 
 * Purpose: Analyze PHY state of nodes when DS-CTS is transmitted.
 * Categorize missed receptions into: TX-busy, RX-busy, CCA-busy, IDLE-miss (SINR/Collision).
 */

#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/internet-module.h"
#include "ns3/wifi-module.h"
#include "ns3/mobility-module.h"
#include "ns3/applications-module.h"

#include <iostream>
#include <vector>
#include <map>
#include <iomanip>

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("PedcaDsctAnalysis");

// Global data
std::vector<Ptr<WifiPhy>> g_phys;

struct DsCtsEvent {
    Time timestamp;
    uint32_t senderId;
    int txBusy = 0;
    int rxBusy = 0;
    int ccaBusy = 0;
    int idle = 0;     // Initially idle
    int success = 0;  // Successfully received header
    
    // Derived metrics
    int idleMiss() const { return std::max(0, idle - success); }
};

std::vector<DsCtsEvent> g_events;
// Map timestamp (us) to index in g_events
std::map<int64_t, std::vector<int>> g_eventMap; 

// Helper to find relevant events for a reception time
// We search backwards from 'now' for recent DS-CTS transmissions
// Helper to find relevant events - Forward Declaration
void RecordSuccess(Time now);

// Callback: PHY TX Begin (Sender side)
// Context: Node ID
void PhyTxBeginCb(std::string context, Ptr<const Packet> packet, double txPowerW)
{
    WifiMacHeader hdr;
    packet->PeekHeader(hdr);
    
    // Identify DS-CTS: CTS Control Frame to specific address
    if (hdr.IsCts() && hdr.GetAddr1() == Mac48Address("00:0F:AC:47:43:00")) {
        uint32_t senderId = std::stoi(context.substr(10)); // Extract ID from "/NodeList/X/..."
        
        DsCtsEvent evt;
        evt.timestamp = Simulator::Now();
        evt.senderId = senderId;
        
        // Scan all other PHYs
        for (auto phy : g_phys) {
            uint32_t nid = phy->GetDevice()->GetNode()->GetId();
            if (nid == senderId) continue;
            
            if (phy->IsStateTx()) evt.txBusy++;
            else if (phy->IsStateRx()) evt.rxBusy++;
            else if (phy->IsStateCcaBusy()) evt.ccaBusy++; // Busy but not RXing (interference)
            else evt.idle++;
        }
        
        // Store event
        int64_t tUs = evt.timestamp.GetMicroSeconds();
        g_events.push_back(evt);
        g_eventMap[tUs].push_back(g_events.size() - 1);
    }
}

// Callback: PHY RX MAC Header End (Receiver side - Success)
void PhyRxMacHeaderEndCb(std::string context, const WifiMacHeader& hdr, const WifiTxVector& txVector, Time psr)
{
    if (hdr.IsCts() && hdr.GetAddr1() == Mac48Address("00:0F:AC:47:43:00")) {
        // Log meaningful info
        std::cout << "RX Success at " << Simulator::Now().GetMicroSeconds() << "us\n";
        RecordSuccess(Simulator::Now());
    }
}

// Helper to find relevant events
void RecordSuccess(Time now) {
    int64_t nowUs = now.GetMicroSeconds();
    bool found = false;
    for (int64_t t = nowUs; t > nowUs - 200; t--) {
         if (g_eventMap.count(t)) {
             found = true;
             for (int idx : g_eventMap[t]) {
                 g_events[idx].success++;
             }
             // Don't return, keep updating collision events if multiple?
             // Actually, usually we map to ONE event or set of colliding events.
             break;
         }
    }
    // if (!found) std::cout << "Warning: Orphaned successful Rx at " << nowUs << "us\n";
}

int main(int argc, char* argv[])
{
    uint32_t nSta = 10;
    double simTime = 2.0;
    std::string dataRate = "0.5Mbps"; // VO Load
    bool backgroundTraffic = false;   // Add BE/VI traffic?
    bool verbose = false;

    CommandLine cmd(__FILE__);
    cmd.AddValue("nSta", "Number of stations", nSta);
    cmd.AddValue("simTime", "Simulation time", simTime);
    cmd.AddValue("dataRate", "VO Data Rate", dataRate);
    cmd.AddValue("bgTraffic", "Enable background traffic (Baseline)", backgroundTraffic);
    cmd.AddValue("verbose", "Verbose logging", verbose);
    cmd.Parse(argc, argv);
    
    if (verbose) {
        LogComponentEnable("PedcaDsctAnalysis", LOG_LEVEL_INFO);
    }

    NodeContainer wifiStaNodes;
    wifiStaNodes.Create(nSta);
    NodeContainer wifiApNode;
    wifiApNode.Create(1);

    YansWifiChannelHelper channel = YansWifiChannelHelper::Default();
    YansWifiPhyHelper phy;
    phy.SetChannel(channel.Create());
    phy.Set("ChannelSettings", StringValue("{36, 20, BAND_5GHZ, 0}"));
    
    WifiHelper wifi;
    wifi.SetStandard(WIFI_STANDARD_80211n);
    wifi.SetRemoteStationManager("ns3::ConstantRateWifiManager",
                                 "ControlMode", StringValue("HtMcs0")); // Ensure robust control frames
    
    WifiMacHelper mac;
    Ssid ssid = Ssid("pedca-collision-test");

    // Install AP
    mac.SetType("ns3::ApWifiMac", "Ssid", SsidValue(ssid), "QosSupported", BooleanValue(true));
    NetDeviceContainer apDevices = wifi.Install(phy, mac, wifiApNode);
    
    // Install STAs with P-EDCA
    mac.SetType("ns3::StaWifiMac", 
                "Ssid", SsidValue(ssid), 
                "QosSupported", BooleanValue(true),
                "PedcaSupported", BooleanValue(true),
                "ActiveProbing", BooleanValue(false));
    NetDeviceContainer staDevices = wifi.Install(phy, mac, wifiStaNodes);

    // Save PHY pointers for global access
    // AP is Node 0
    Ptr<WifiNetDevice> apDev = DynamicCast<WifiNetDevice>(apDevices.Get(0));
    if (apDev) {
        g_phys.push_back(apDev->GetPhy());
    }

    // STAs are 1..nSta
    for (uint32_t i = 0; i < nSta; ++i) {
        Ptr<WifiNetDevice> staDev = DynamicCast<WifiNetDevice>(staDevices.Get(i));
        if (staDev) {
            g_phys.push_back(staDev->GetPhy());
        }
    }

    // Connect Traces
    Config::Connect("/NodeList/*/DeviceList/*/$ns3::WifiNetDevice/Phy/PhyTxBegin", MakeCallback(&PhyTxBeginCb));
    Config::Connect("/NodeList/*/DeviceList/*/$ns3::WifiNetDevice/Phy/PhyRxMacHeaderEnd", MakeCallback(&PhyRxMacHeaderEndCb));

    // Mobility
    MobilityHelper mobility;
    mobility.SetMobilityModel("ns3::ConstantPositionMobilityModel");
    Ptr<ListPositionAllocator> posAlloc = CreateObject<ListPositionAllocator>();
    posAlloc->Add(Vector(0.0, 0.0, 0.0)); // AP
    for (uint32_t i = 0; i < nSta; ++i) {
        // Star topology, 5m distance
        double angle = 2.0 * M_PI * i / nSta;
        posAlloc->Add(Vector(5.0 * cos(angle), 5.0 * sin(angle), 0.0));
    }
    mobility.SetPositionAllocator(posAlloc);
    mobility.Install(wifiApNode);
    mobility.Install(wifiStaNodes);

    // Internet Stack
    InternetStackHelper stack;
    stack.Install(wifiApNode);
    stack.Install(wifiStaNodes);
    Ipv4AddressHelper address;
    address.SetBase("192.168.1.0", "255.255.255.0");
    address.Assign(apDevices);
    Ipv4InterfaceContainer staIf = address.Assign(staDevices);

    // Traffic Setup
    uint16_t port = 9000;
    UdpServerHelper server(port);
    server.Install(wifiApNode.Get(0)).Start(Seconds(0.0));

    // VO Traffic
    DataRate voRate(dataRate);
    uint32_t payloadSize = 1000;
    double voPps = voRate.GetBitRate() / (8.0 * payloadSize);
    Time voInterval = Seconds(1.0 / voPps);

    for (uint32_t i = 0; i < nSta; ++i) {
        UdpClientHelper client(staIf.GetAddress(i), port);
        client.SetAttribute("PacketSize", UintegerValue(payloadSize));
        client.SetAttribute("Interval", TimeValue(voInterval));
        client.SetAttribute("Tos", UintegerValue(0xC0)); // AC_VO
        client.SetAttribute("StartTime", TimeValue(Seconds(0.1 + i*0.001))); // Slight stagger
        client.SetAttribute("StopTime", TimeValue(Seconds(simTime)));
        client.Install(wifiStaNodes.Get(i));
        
        if (backgroundTraffic) {
             // Add BE Traffic (AC_BE)
             UdpClientHelper bgClient(staIf.GetAddress(i), port);
             bgClient.SetAttribute("PacketSize", UintegerValue(1000));
             bgClient.SetAttribute("Interval", TimeValue(voInterval)); // Same load
             bgClient.SetAttribute("Tos", UintegerValue(0x00)); // AC_BE
             bgClient.SetAttribute("StartTime", TimeValue(Seconds(0.2 + i*0.001)));
             bgClient.SetAttribute("StopTime", TimeValue(Seconds(simTime)));
             bgClient.Install(wifiStaNodes.Get(i));
        }
    }

    Simulator::Stop(Seconds(simTime + 0.1));
    Simulator::Run();
    
    // Aggregation Logic (Verify A)
    long totalRecvPotential = 0;
    long totalTxBusy = 0;
    long totalRxBusy = 0;
    long totalCcaBusy = 0;
    long totalIdleMiss = 0;
    long totalSuccess = 0;
    int eventCount = 0;
    
    // Filter for Steady State 
    Time steadyStateStart = Seconds(0.1);
    
    for (const auto& evt : g_events) {
        if (evt.timestamp < steadyStateStart) continue;
        
        eventCount++;
        totalTxBusy += evt.txBusy;
        totalRxBusy += evt.rxBusy;
        totalCcaBusy += evt.ccaBusy;
        totalIdleMiss += evt.idleMiss();
        totalSuccess += evt.success;
        totalRecvPotential += (evt.txBusy + evt.rxBusy + evt.ccaBusy + evt.idle);
    }
    
    std::cout << "\n=== Verification A: Breakdown of Missed DS-CTS (Steady State t > " << steadyStateStart.GetSeconds() << "s) ===\n";
    std::cout << "Analysis of " << eventCount << " DS-CTS transmission events.\n";
    std::cout << "Total Potential Receivers: " << totalRecvPotential << "\n\n";
    
    double pTx = 100.0 * totalTxBusy / totalRecvPotential;
    double pRx = 100.0 * totalRxBusy / totalRecvPotential;
    double pCca = 100.0 * totalCcaBusy / totalRecvPotential;
    double pMiss = 100.0 * totalIdleMiss / totalRecvPotential;
    double pSucc = 100.0 * totalSuccess / totalRecvPotential;
    
    std::cout << std::fixed << std::setprecision(2);
    std::cout << "  Success (Updated NAV): " << pSucc << "% (" << totalSuccess << ")\n";
    std::cout << "  Miss - RX Busy:        " << pRx << "% (" << totalRxBusy << ")\n";
    std::cout << "  Miss - TX Busy:        " << pTx << "% (" << totalTxBusy << ")\n";
    std::cout << "  Miss - CCA Busy:       " << pCca << "% (" << totalCcaBusy << ")\n";
    std::cout << "  Miss - IDLE (Crash):   " << pMiss << "% (" << totalIdleMiss << ") <- Likely Collision/SINR\n\n";
    
    std::cout << "Avg Receivers per DS-CTS: " << (double)totalSuccess / eventCount << "\n";

    Simulator::Destroy();
    return 0;
}
