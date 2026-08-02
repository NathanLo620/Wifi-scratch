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
#include <set>
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
    std::set<uint32_t> idleNodes; // node IDs that were genuinely idle (no unrelated activity) at TX begin

    // Derived metrics
    int idleMiss() const { return std::max(0, idle - success); }
};

std::vector<DsCtsEvent> g_events;
// Map timestamp (us) to index in g_events
std::map<int64_t, std::vector<int>> g_eventMap;

// Exact-timestamp (ns) -> indices into g_events of DS-CTS TXs that began at that instant.
// Used to identify TRUE collision groups (>=2 simultaneous senders) vs isolated single-sender events.
std::map<int64_t, std::vector<int>> g_txGroups;
// Receiver node ID -> list of timestamps (ns) at which it successfully decoded a DS-CTS MAC header.
std::map<uint32_t, std::vector<int64_t>> g_successByNode;
// (nodeId, startTimeNs) for EVERY PHY transmission of ANY type (not just DS-CTS) -- used to check
// whether a bystander's DS-CTS decode failure is explained by some other TX starting mid-flight
// (either the bystander itself going half-duplex, or a third-party interferer), which would NOT
// have been visible in the TX-begin-instant snapshot.
std::vector<std::pair<uint32_t, int64_t>> g_allTxBegins;

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

    uint32_t txNodeId = std::stoi(context.substr(10));
    g_allTxBegins.push_back({txNodeId, Simulator::Now().GetNanoSeconds()});

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
            else { evt.idle++; evt.idleNodes.insert(nid); }
        }

        // Store event
        int64_t tUs = evt.timestamp.GetMicroSeconds();
        int64_t tNs = evt.timestamp.GetNanoSeconds();
        g_events.push_back(evt);
        int idx = g_events.size() - 1;
        g_eventMap[tUs].push_back(idx);
        g_txGroups[tNs].push_back(idx);
    }
}

// Callback: PHY RX MAC Header End (Receiver side - Success)
void PhyRxMacHeaderEndCb(std::string context, const WifiMacHeader& hdr, const WifiTxVector& txVector, Time psr)
{
    if (hdr.IsCts() && hdr.GetAddr1() == Mac48Address("00:0F:AC:47:43:00")) {
        uint32_t receiverId = std::stoi(context.substr(10)); // Extract ID from "/NodeList/X/..."
        // Log meaningful info
        std::cout << "RX Success at " << Simulator::Now().GetMicroSeconds() << "us (node " << receiverId << ")\n";
        RecordSuccess(Simulator::Now());
        g_successByNode[receiverId].push_back(Simulator::Now().GetNanoSeconds());
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

    // Required for the PhyRxMacHeaderEnd trace (used below to detect successful
    // reception of the DS-CTS MAC header) to actually fire; defaults to false.
    Config::SetDefault("ns3::WifiPhy::NotifyMacHdrRxEnd", BooleanValue(true));

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

    // Verification B: given an ACTUAL collision (>=2 simultaneous DS-CTS senders, still zero
    // legacy STAs in this topology), can a bystander (AP or STA not itself transmitting DS-CTS
    // at that instant) still decode ONE of the colliding DS-CTS frames and update its NAV?
    std::set<uint32_t> allNodeIds;
    for (auto phy : g_phys) {
        allNodeIds.insert(phy->GetDevice()->GetNode()->GetId());
    }

    long collisionGroups = 0, totalBystandersColl = 0, successBystandersColl = 0;
    long singleGroups = 0, totalBystandersSingle = 0, successBystandersSingle = 0;
    // Same "busy at other traffic" caveat as Verification A, but tracked per group here:
    long collisionGroupsBusyBystanders = 0, singleGroupsBusyBystanders = 0;
    const int64_t kMatchWindowNs = 100000; // 100 us: covers DS-CTS airtime + propagation
    // (tNs, senderId, failedBystanderNid) for isolated (non-colliding) groups only, feeds Verification C
    std::vector<std::tuple<int64_t, uint32_t, uint32_t>> g_isolatedFailures;

    for (const auto& [tNs, indices] : g_txGroups) {
        if (NanoSeconds(tNs) < steadyStateStart) continue;

        std::set<uint32_t> senderSet;
        std::set<uint32_t> idleNodes; // genuinely idle (no unrelated TX/RX/CCA activity) at this instant
        for (int idx : indices) {
            senderSet.insert(g_events[idx].senderId);
            idleNodes.insert(g_events[idx].idleNodes.begin(), g_events[idx].idleNodes.end());
        }
        bool isCollision = senderSet.size() >= 2;

        for (uint32_t nid : allNodeIds) {
            if (senderSet.count(nid)) continue; // exclude participants in this DS-CTS TX group
            if (!idleNodes.count(nid)) {
                // This bystander was itself busy with unrelated traffic (TX/RX/CCA) at TX-begin;
                // exclude it, otherwise a "decode failure" here just reflects general channel
                // load, not the DS-CTS collision/reception question being asked.
                isCollision ? collisionGroupsBusyBystanders++ : singleGroupsBusyBystanders++;
                continue;
            }

            bool succeeded = false;
            auto it = g_successByNode.find(nid);
            if (it != g_successByNode.end()) {
                for (int64_t st : it->second) {
                    if (st >= tNs && st <= tNs + kMatchWindowNs) {
                        succeeded = true;
                        break;
                    }
                }
            }

            if (isCollision) {
                totalBystandersColl++;
                if (succeeded) successBystandersColl++;
            } else {
                totalBystandersSingle++;
                if (succeeded) successBystandersSingle++;
                if (!succeeded) {
                    g_isolatedFailures.push_back({tNs, *senderSet.begin(), nid});
                }
            }
        }
        isCollision ? collisionGroups++ : singleGroups++;
    }

    std::cout << "\n=== Verification B: Bystander decode outcome, split by collision vs isolated ===\n";
    std::cout << "(bystanders busy with unrelated TX/RX/CCA at that instant are excluded from the\n";
    std::cout << " denominator below -- only genuinely idle bystanders are counted)\n";
    std::cout << "Collision groups (>=2 simultaneous DS-CTS senders, 0 legacy STAs present): "
              << collisionGroups << " (excluded " << collisionGroupsBusyBystanders
              << " busy-bystander samples)\n";
    if (totalBystandersColl > 0) {
        std::cout << "  Idle-bystander NAV-decode success: " << successBystandersColl << "/"
                  << totalBystandersColl << " (" << (100.0 * successBystandersColl / totalBystandersColl)
                  << "%)\n";
    } else {
        std::cout << "  (no collision groups observed in steady state)\n";
    }
    std::cout << "Isolated single-sender groups: " << singleGroups << " (excluded "
              << singleGroupsBusyBystanders << " busy-bystander samples)\n";
    if (totalBystandersSingle > 0) {
        std::cout << "  Idle-bystander NAV-decode success: " << successBystandersSingle << "/"
                  << totalBystandersSingle << " ("
                  << (100.0 * successBystandersSingle / totalBystandersSingle) << "%)\n";
    } else {
        std::cout << "  (no isolated groups observed in steady state)\n";
    }

    // Verification C: for isolated (non-colliding) single-sender DS-CTS that a genuinely-idle
    // bystander still failed to decode, was that failure explained by SOME OTHER transmission
    // (the bystander itself going half-duplex, or a third-party interferer) starting mid-flight,
    // i.e. after the TX-begin snapshot but before/during the DS-CTS's own airtime? If so, the
    // miss isn't a "clean-channel SINR crash" -- it's just that the channel didn't stay idle for
    // the whole airtime, which the instantaneous snapshot at TX-begin can't see.
    const int64_t kDsCtsAirtimeNs = 50000; // ~50 us: conservative upper bound on 6 Mbps non-HT CTS airtime
    long explainedBySelfTx = 0, explainedByThirdParty = 0, unexplained = 0;
    for (const auto& [tNs, senderId, nid] : g_isolatedFailures) {
        bool self = false, third = false;
        for (const auto& [txNode, txTimeNs] : g_allTxBegins) {
            if (txTimeNs <= tNs || txTimeNs > tNs + kDsCtsAirtimeNs) continue; // must start mid-flight
            if (txNode == senderId) continue; // the DS-CTS sender re-transmitting isn't relevant here
            if (txNode == nid) self = true;
            else third = true;
        }
        if (self) explainedBySelfTx++;
        else if (third) explainedByThirdParty++;
        else unexplained++;
    }
    long totalIsolatedFailures = (long)g_isolatedFailures.size();
    std::cout << "\n=== Verification C: Root cause of isolated-DS-CTS bystander failures ===\n";
    std::cout << "Total isolated-group bystander failures analyzed: " << totalIsolatedFailures << "\n";
    if (totalIsolatedFailures > 0) {
        std::cout << "  Explained - bystander itself started TX mid-flight (half-duplex): "
                  << explainedBySelfTx << " (" << (100.0 * explainedBySelfTx / totalIsolatedFailures) << "%)\n";
        std::cout << "  Explained - third-party node started TX mid-flight (new interferer): "
                  << explainedByThirdParty << " (" << (100.0 * explainedByThirdParty / totalIsolatedFailures)
                  << "%)\n";
        std::cout << "  Unexplained (idle for full ~" << (kDsCtsAirtimeNs / 1000)
                  << "us window, still failed -> pure path-loss/SNR/preamble-detect miss): " << unexplained
                  << " (" << (100.0 * unexplained / totalIsolatedFailures) << "%)\n";
    }

    Simulator::Destroy();
    return 0;
}
