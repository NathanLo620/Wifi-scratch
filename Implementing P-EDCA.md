Implementation Plan: Strict P-EDCA (Two-Stage Access)
Target Component: src/wifi/model/qos-frame-exchange-manager & src/wifi/model/qos-txop

Objective
Implement a "Strict P-EDCA" mechanism where a P-EDCA capable station transmits a Defer Signal (DS-CTS) immediately upon gaining channel access, followed by a mandatory second contention window (AIFS + Backoff) before transmitting data.

Requirements
Trigger: Enabled only if PedcaSupported attribute is true and Traffic is AC_VO.
Stage 1 (DS-CTS):
Transmit a CTS-to-Self frame.
Duration Field: Fixed at 97 us.
Action: Do NOT transmit data immediately after SIFS. Release the channel state machine to force a new backoff.
Stage 2 (P-EDCA Contention):
Parameters: Override EDCA parameters for this specific backoff:
CWmin = 7
CWmax = 7
AIFSN = 2
Backoff: Standard random backoff within [0, 7].
Timing Result: The gap between DS-CTS End and Data Start will be SIFS(16us) + Slot(9us) * (AIFSN(2) + Backoff[0..7]).
Data Transmission: Once the second backoff expires, proceed with normal RTS/CTS (if enabled) and Data transmission.
Restoration: Restore original EDCA parameters (CWmin/max, AIFSN) after the P-EDCA TXOP finishes.
Detailed Modifications
1. src/wifi/model/qos-frame-exchange-manager.h
Goal: Add state variables to track the two-stage process.

// Add to class QosFrameExchangeManager private members:
bool m_pedcaPending;          // True if DS-CTS was sent and we are in P-EDCA backoff
bool m_pedcaOriginalParamsSaved; // To ensure we don't save over already saved params
// Backup variables to restore original EDCA params
uint32_t m_savedCwMin;
uint32_t m_savedCwMax;
uint8_t m_savedAifsn;
2. src/wifi/model/qos-frame-exchange-manager.cc
Modification A: StartTransmission (The Intercept)
Locate the StartTransmission method (or the point where a TXOP is started). We need to intercept AC_VO transmissions.

// In StartTransmission(edca), BEFORE checking for SendRts or SendData:
if (edca->GetAc() == AC_VO && GetWifiMac()->GetAttribute("PedcaSupported") /* pseudocode */) 
{
    if (!m_pedcaPending) 
    {
        // === Stage 1: Send DS-CTS ===
        
        // 1. Save Original Parameters
        if (!m_pedcaOriginalParamsSaved) {
             m_savedCwMin = edca->GetMinCw();
             m_savedCwMax = edca->GetMaxCw();
             m_savedAifsn = edca->GetAifsn();
             m_pedcaOriginalParamsSaved = true;
        }
        // 2. Construct DS-CTS
        WifiMacHeader ctsHeader;
        ctsHeader.SetType(WIFI_MAC_CTL_CTS);
        ctsHeader.SetDsNotFrom();
        ctsHeader.SetMoreFragments(false);
        ctsHeader.SetRetry(false);
        ctsHeader.SetAddr1(GetWifiMac()->GetBssid()); // RA = BSSID (CTS-to-Self behavior)
        ctsHeader.SetDuration(MicroSeconds(97));      // <--- STRICT REQUIREMENT: 97us
        // 3. Transmit CTS immediately
        // Use SendNormalCts or manually call SendCtlFrame. 
        // CRITICAL: Pass '0' or appropriate flag to indicate NO immediate response expected.
        // We want the PHY to transmit, but the MAC to go back to IDLE/Backoff after TxEnd.
        
        SendCtsToSelf(ctsHeader, ...); // You may need to adapt this call to NOT trigger protection logic
        
        // 4. Update State & Override Parameters for Stage 2
        m_pedcaPending = true;
        
        // Force P-EDCA Params
        edca->SetMinCw(7);
        edca->SetMaxCw(7);
        edca->SetAifsn(2);
        
        // 5. Force Start New Backoff
        // This effectively "throws away" the current TXOP opportunity to wait for the new backoff
        edca->StartAccessIfNeeded(); // Or explicitly InvokeBackoff
        
        // 6. Return early (Do NOT send data yet)
        return; 
    }
    else
    {
        // === Stage 2: P-EDCA Backoff Finished ===
        // m_pedcaPending is true, meaning we just finished the P-EDCA backoff.
        // Proceed to normal data transmission flow below...
    }
}
Modification B: TransmissionSucceeded / TransmissionFailed (Restoration)
When the Data transmission matches (or fails), we must reset the state and restore parameters.

// In TransmissionSucceeded and TransmissionFailed callbacks:
if (m_pedcaPending && edca->GetAc() == AC_VO)
{
    // Restore Original Parameters
    if (m_pedcaOriginalParamsSaved) {
        edca->SetMinCw(m_savedCwMin);
        edca->SetMaxCw(m_savedCwMax);
        edca->SetAifsn(m_savedAifsn);
        m_pedcaOriginalParamsSaved = false;
    }
    m_pedcaPending = false;
}
3. src/wifi/model/qos-txop.h / 
.cc
 (Optional helper)
Ensure SetMinCw, SetMaxCw, SetAifsn are public or accessible to QosFrameExchangeManager. They usually are.


Verification Logic for Implementer
Check PCAP:
Find CTS (RA=AP). Duration should be 97us.
Find Data (TID=VO).
Calculate Delta = Data.Start - CTS.End.
Delta must be >= 34us (SIFS+AIFS+minBackoff).
Delta must be <= 97us (SIFS+AIFS+maxBackoff).
Delta should never be exactly 16us (SIFS only).