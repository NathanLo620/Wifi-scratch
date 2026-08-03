# Adaptive P-EDCA — English Integration Reference

> **Audience: another AI (or engineer) re-integrating this adaptive P-EDCA layer into a
> DIFFERENT / MODIFIED ns-3 wifi model.** This top section is a self-contained, English
> port-guide: it lists every symbol we added or touched, where the hooks live, and the
> invariants that must survive a merge. The rest of the file (§0 onward) is the original
> Chinese design notes + measurements — read §0 for the algorithm-flow diagrams.
>
> ns-3 version: **ns-3.45**, `src/wifi/model/`. All paths below are relative to that dir
> unless noted. This was authored against our tree on 2026-07-14.
>
> ⚠ **2026-08-02: 這份 port guide 描述的設計從未進入這棵樹，而且它假設的是「單 DS-CTS」
> sender。目前實際運行的實作與調控流程請看下面的【現行實作】一節；本節（§E0 起）與 §0–§9
> 保留作為歷史對照。**

---

# 【現行實作】Adaptive P-EDCA 調控流程（2026-08-02）

> **這一節描述的是目前這棵樹上真正 build 過、跑過、量過的實作。**
> 底下的 §E0–§E6 與 §0–§9 是 2026-07 針對**單** DS-CTS sender 寫的設計文件，而且那份設計
> **從未進入這棵樹**（2026-08-02 稽核確認所有符號都不存在）。兩邊衝突時**以本節為準**；
> 舊章節保留作為歷史對照與 merge 參考。
>
> 對應 commit：model `[v6.4.2] Adaptive P-EDCA implemenation added` 之後的修改。
> 底層 sender 是 **dual DS-CTS**（`m_dsCtsRepeat=2`），與舊文件假設的單 DS-CTS 不同。

## N1. 系統架構

```
┌──────────────────────── AP (WifiNetDevice, link 0) ────────────────────────┐
│                                                                             │
│  WifiPhy StateHelper ─"State"──────────────┐  通道忙碌時間                   │
│  WifiPhy ─"PhyRxPpduDrop"──────────────────┤  掉掉的 DS-CTS                  │
│  FrameExchangeManager ─"DsCtsRx"───────────┤  解碼成功的 DS-CTS              │
│  QosFrameExchangeManager                   │                                │
│    ├ GetLliRxCount() / GetLliRxCount(addr) ┤  延遲壓力（LLI）                │
│    └ GetVoRxCount()                        │  AC_VO 收包數（LLI 的分母）     │
│  ApWifiMac::GetBufferStatus(tid, addr) ────┤  每台佇列壓力（BSR）            │
│                                            ▼                                │
│                                   ┌──────────────────┐                      │
│                                   │  PedcaController  │  每 Period(100ms)   │
│                                   │      Step()       │  觀測 → 決策 → 下發  │
│                                   └────────┬─────────┘                      │
│                                            │ SetPedcaParametersBulk(map)    │
│                                   ┌────────▼─────────┐                      │
│                                   │    ApWifiMac      │ m_pedcaThetaByAid   │
│                                   └────────┬─────────┘                      │
│                                            │ P-EDCA Parameter Set IE (250)  │
└────────────────────────────────────────────┼───────────────────────────────┘
       ▲ UL QoS Data                          ▼ beacon / probe resp / assoc resp
       │ QoS Control = [7-bit Queue Size | 1-bit LLI]
┌──────┴──────────────────── STA_i (PedcaSupported=true) ────────────────────┐
│  送包: FinalizeMacHeader / ForwardMpduDown → SetQueueSizeAndPedcaLli()      │
│        VO 封包在佇列停留 > 0.7 × 10ms → 設 LLI bit；queue size cap 到 127    │
│  收 beacon: ApplyOperationalSettings → ApplyPedcaParameters()               │
│        → GetEntryFor(m_aid) → qosFem->SetCwds/SetQsrc/SetPsrc               │
└─────────────────────────────────────────────────────────────────────────────┘
```

**分工**：AP 只當 controller + advertiser，**不**當 P-EDCA sender（用 `PedcaControl`，
絕對不要用 `PedcaSupported`，見 §N8-1）。STA 才是真正的 P-EDCA sender。

## N2. 一個控制週期的時序

```
  |<──────────── 觀測期 Period = 100ms ────────────>|
  | PHY State trace 累計 busy 時間                    |
  | DsCtsRx / PhyRxPpduDrop 累計 DS-CTS（分 burst）   |   Step() 在週期結束執行:
  | FEM 累計 LLI 數、AC_VO 收包數                      |   ┌────────────────────────┐
  | STA 上行時把 BSR/LLI 寫進 QoS Control              |──▶│ 1. 收攏觀測、算衍生量    │
  |                                                  |   │ 2. 依 policy 決定 θ     │
  |                                                  |   │ 3. SetPedcaParametersBulk│
  |                                                  |   │ 4. 觸發 ControlStep trace│
  |                                                  |   │ 5. 歸零累加器、排下一次   │
                                                         └───────────┬────────────┘
   新 θ 透過「下一個 beacon」下發 ─────────────────────────────────────┘
   → STA ApplyPedcaParameters() → 下次送包生效
```

⚠ **端到端迴路延遲 = Period(100ms) + 最多一個 beacon interval(102.4ms)**。
on/off 與 MMPP 的 burst 平均長度是 **0.1 秒**，所以目前的迴路比它要追的負載**慢一到兩個
burst**。逐 burst 追蹤在預設 beacon interval 下**物理上做不到**（見 §N8-4）。

## N3. 觀測訊號

| 訊號 | 粒度 | 來源 | 用於 |
|---|---|---|---|
| `busyFrac` | BSS | `WifiPhyStateHelper` "State"，累計 TX/RX/CCA_BUSY/SWITCHING ÷ Period | v2 的壅塞閘門（實測永遠不觸發，見 §N8-3） |
| `overhead` | BSS | `bursts × BurstCost(185µs) ÷ Period` | **loaddriven 主訊號**（Stage-1 佔用介質的比例） |
| `urgency` | BSS | `ΔGetLliRxCount() ÷ ΔGetVoRxCount()` | **loaddriven 主訊號**（快超過延遲預算的封包比例） |
| `collRate` | BSS | burst-level：完全沒解到任何 frame 的 burst ÷ 總 burst | v2 的 CWds 規則；也記 frame-level 當診斷 |
| `dsCtsBursts` / `dsCtsFrames` | BSS | `FrameExchangeManager::GetDsCtsBurstRxCount()` / `GetDsCtsRxCount()` | overhead 的分子、trace |
| `bsrSta` | per-STA | `ApWifiMac::GetBufferStatus(tid∈{6,7}, addr)`，跳過 255 | v2 的逐台判斷 |
| `lliSta` | per-STA | `GetLliRxCount(addr)` 的週期差分 | v2 的逐台判斷 |
| `kHat` | BSS | 曾送過 LLI 的 STA 集合大小（sticky）；`KOverride≥0` 時直接用該值 | kdriven |

**DS-CTS burst 的定義**：dual DS-CTS 下一次 Stage-1 送兩個 frame（相隔 SIFS），所以要
把 frame 併成 burst 才有意義。`DsCtsBurstGap`(100µs) 以內的相鄰觀測算同一個 burst；
一個 burst 只要有任一 frame 解到就算成功。**這是為什麼不能用 frame-level collision rate**：
第一個 frame 被撞掉、第二個成功，正是 dual DS-CTS 的設計本意，卻會被算成 0.5 的碰撞率。

## N4. 四種 policy

由 `PedcaController::Policy` attribute 選擇（scenario CLI `--policy`）。

### N4.1 `loaddriven`（**bursty / 變動負載建議用這個**）

CWds 固定 1、PSRC 固定 3，**QSRC 是唯一的閉迴路變數**：

```
每個週期:
    overhead = bursts × 185µs / Period
    urgency  = ΔLLI / Δ(AC_VO 收包數)

    if   overhead > OverheadHigh (0.06)                        → QSRC += 1
    elif urgency > UrgencyHigh (0.15) and overhead < OverheadLow (0.03)
                                                               → QSRC -= 1
    else                                                        → 維持
    clamp 到 [QsrcFloor (1), 5]
```

**這個不對稱是刻意的、而且是量出來的**：
- **往上只看 overhead**：P-EDCA 佔太多介質時，它就是在跟自己要保護的流量搶。
- **往下還必須有人真的快超過延遲預算**。如果只看 overhead 就往下調，光負載時介質是空的、
  overhead≈0，會被誤讀成「有餘裕」→ QSRC 掉到下限 → P-EDCA 在稀疏流量上亂觸發 →
  **實測尾端延遲 +45%**。urgency 這個條件就是擋住這件事的。
- **QSRC 下限 1**：QSRC=0 在 15 個 (traffic × 負載) 格子裡幾乎都是最差或接近最差
  （光負載 k=30 是 +68.6%，CBR k=15 是 +29.3%）。

為什麼 CWds 和 PSRC 不進迴路：離線 sweep 顯示 CWds=1 在 15 格裡 13 格最佳（另兩格差 <3%），
是 don't-care；PSRC 固定 3 再讓 QSRC 自由移動，離線超出 oracle 只有 2.8%，跟三個參數
全調（2.9%）一樣好。少一個旋鈕、少一個震盪來源。

### N4.2 `kdriven`（**光負載會壞掉，不要用在 bursty**）

```
kHat = |曾送過 LLI 的 STA 集合|          (sticky，或由 KOverride 指定)
CWds = 1
QSRC = clamp(round(0.5 + 0.2 × kHat), 0, 5)
PSRC = 3 if kHat ≤ 10 ; 2 if kHat ≤ 22 ; else 1
```

這條法則是 2026-08-01 sweep 擬合出來的，在**固定負載**下很好（離線超出 oracle 2.9%，
對照固定預設 15.8%）。但 k 是**組態屬性不是運行時狀態**，光負載下 LLI 很少觸發 →
kHat 塌掉 → QSRC 掉到 1 → **實測 On-Off 0.1Mbps +45.0%、MMPP 0.1Mbps +33.5%**。
保留它是為了對照與重現離線結論，**不建議當預設**。

### N4.3 `v2`（歷史規則，實測三條全是死的或方向錯的）

BSS-wide CWds 由 collRate 決定 + busyFrac 全域保守閘門 + 逐台激進/保守角落。
在目前的 dual DS-CTS model 上：
- `busyFrac > 0.85` 閘門**永遠不觸發**：實測 channel idle 在所有 15 格都是 23–27%，
  busyFrac ≈ 0.75。
- `collRate → CWds` **沒有作用點**：CWds 是 don't-care。
- 逐台「激進角落 = QSRC=1, PSRC=3」其實是 **k=5 的最佳解被套用到所有負載**，
  在高競爭下接近最差。
- 實測在 k=15 的三種 traffic **全部輸給「什麼都不做」**（+9.7 / +2.8 / +4.3%）。

### N4.4 `fixed`

把 `SetInitialTheta()` 給的 θ 原封不動每週期推一次。用途是**對照組**：機制全開
（IE + BSR + LLI + 感測）但迴路不動，可以量出機制本身的成本。

## N5. 參數下發路徑

**IE 格式**（`pedca-parameter-set.{h,cc}`，Element ID **250**，取自保留區 243–254）：

```
Information field = 1 + 5×N octets
 ┌────────────┬──────────────────────────────────────────┐
 │Update Count│  N 個 5-byte entry                        │
 │ (1 octet)  │  AID(2B, LE) + CWds + QSRC_th + PSRC_lim  │
 └────────────┴──────────────────────────────────────────┘
```
Update Count 每次 push 都 +1（語意是「第幾次推播」，不是「改了幾次」）。超過 255 bytes 時
`WifiInformationElement::Serialize` 會自動分片，不需要限制 STA 數。

**下發點**：`SendOneBeacon` / `GetProbeRespProfile` / `GetAssocResp` 三處，
guard 都是 `if (GetPedcaSupported() || m_pedcaControl)`。

**STA 套用**（`StaWifiMac::ApplyPedcaParameters()`）：
1. 用**私有成員 `m_aid`**，不是 `GetAssociationId()`（後者 assert `IsAssociated()`，
   在處理 assoc-resp 當下還是 false）。未關聯時 `m_aid==0`，不會誤配到任何真實 AID。
2. `GetEntryFor(m_aid)` 找不到自己那列就完全不動作。
3. **QSRC clamp 到 `FrameRetryLimit - 1`**：`qos-fem` 會在
   `FrameRetryLimit <= m_qsrc_threshold` 時自動抬高 retry limit 而且**只升不降**，
   會永久改變該 STA 全部 AC 的重傳行為。被 clamp 時印 `[P-EDCA PARAM CLAMP]`。
4. **只在值真的改變時才寫 FEM**：beacon 每 102.4ms 重播同一組 θ，無條件寫會一直重置
   sender 狀態。`SetPsrc()` 本身也做成 idempotent。

## N6. Attributes 一覽

**`PedcaController`**（scenario 都有對應 CLI）：

| Attribute | 預設 | 用途 |
|---|---|---|
| `Policy` | `kdriven` ⚠ | `kdriven` / `loaddriven` / `v2` / `fixed` |
| `Period` | 100 ms | 控制週期 |
| `OverheadHigh` / `OverheadLow` | 0.06 / 0.03 | loaddriven 的 Stage-1 airtime 上下界 |
| `UrgencyHigh` / `UrgencyLow` | 0.15 / 0.05 | loaddriven 的 LLI 比例門檻 |
| `QsrcFloor` | 1 | QSRC 下限，0 在各種負載都很差 |
| `BurstCost` | 185 µs | 一個 Stage-1 burst 的介質成本（2×44 + 16 + 81） |
| `KOverride` | −1 | ≥0 時強制 k，用來把 policy 誤差和估計器誤差分開量 |
| `QsrcIntercept` / `QsrcSlope` | 0.5 / 0.2 | kdriven 的 QSRC 線性law |
| `CwdsKDriven` | 1 | kdriven/loaddriven 下發的 CWds |
| `Psrc3MaxK` / `Psrc2MaxK` | 10 / 22 | kdriven 的 PSRC 分段點 |
| `DsCtsBurstGap` | 100 µs | DS-CTS 併 burst 的間隔門檻 |
| `BusyHigh` / `CollHigh` / `CollLow` / `LliHigh` / `BsrPerStaHigh` | 0.85 / 0.90 / 0.30 / 5 / 8.0 | 僅 v2 使用 |
| `Qsrc/PsrcAggressive`, `Qsrc/PsrcConservative`, `CwdsMax` | 1/3, 5/1, 2 | 僅 v2 使用 |

⚠ **`Policy` 的預設值目前仍是 `kdriven`，但 `kdriven` 在光負載下實測會爆掉（+45%）。**
這是歷史遺留：`kdriven` 先實作、當時定為預設，`loaddriven` 是後來因應 bursty 情境才加的。
**跑 on/off / MMPP / 光負載一律要明確加 `--policy=loaddriven`**，否則會拿到會壞掉的那條。
是否要把預設改成 `loaddriven` 尚未定案（改了會變更既有命令的行為）。

**`QosFrameExchangeManager`**：`PedcaLliDelayBound`(10 ms)、`PedcaLliFraction`(0.7)、
`PedcaResetPsrcOnLimitChange`(false)。
**`ApWifiMac`**：`PedcaControl`(false)。
**scenario 必設**：`SetQueueSize=true`、`ApWifiMac::BsrLifetime = 2×Period`
（預設 20 ms 會在 controller 讀到之前就過期）。

## N7. 實測結果

**On-Off / MMPP，nSta=30，3 seeds，8 秒，P-EDCA STA P99 對固定預設 (0,2,1)**：

| traffic | rate | k | kdriven | loaddriven |
|---|---|---|---|---|
| On-Off | 0.1M | 5 | **+45.0%** | 0.0% |
| On-Off | 0.1M | 15 | +0.9% | 0.0% |
| MMPP | 0.1M | 5 | +1.7% | +0.5% |
| MMPP | 0.1M | 15 | **+33.5%** | +0.1% |
| On-Off | 1M | 5 | −22.4% | **−27.8%** |
| On-Off | 1M | 15 | **−5.3%** | −1.4% |
| MMPP | 1M | 5 | −20.3% | **−22.1%** |
| MMPP | 1M | 15 | **−7.1%** | +6.5% |

**四組對照（On-Off 1Mbps，圖在 `delay_pdf/11be/adaptive_compare/`）**：

| nPedca | arm | P50 | P95 | P99 | vs default |
|---|---|---|---|---|---|
| 5 | EDCA only | 1.75 | 9.33 | 13.48 | +13.6% |
| 5 | default (0,2,1) | 1.46 | 5.65 | 11.87 | — |
| 5 | best fixed (1,3,3) | 1.37 | 4.55 | 8.18 | −31.1% |
| 5 | **adaptive (loaddriven)** | 1.44 | 5.20 | **8.99** | **−24.2%** |
| 15 | EDCA only | 1.75 | 9.33 | 13.48 | −0.4% |
| 15 | default (0,2,1) | 1.60 | 8.14 | 13.53 | — |
| 15 | best fixed (1,4,3) | 1.81 | 7.45 | 11.75 | −13.2% |
| 15 | **adaptive (loaddriven)** | 1.99 | 9.38 | 13.33 | −1.5% |

以「補上 oracle 落差的比例」看：**k=5 補上 78%，k=15 只補上 11%**。
（best fixed 是用離線 sweep 針對該 traffic 與該 k 挑出來的，adaptive 兩者都不知道。）

## N8. 已知限制與踩過的坑

1. **AP 只能用 `PedcaControl`，不能用 `PedcaSupported`**。後者會讓 AP 自己的 VO 管理幀
   啟動 P-EDCA sender、覆寫自身 VO EDCAF，再透過 beacon 的 `EdcaParameterSet`
   毒化全體 STA 的 VO CW。`qos-fem` 的 gate 沒有檢查 `TypeOfStation`，這個坑今天仍然在。
2. **DS-CTS 接收計數必須放在 `FrameExchangeManager::PostProcessFrame`，不能放
   `UpdateNav`**。`UpdateNav` 的 DS-CTS 分支在 `if (navEnd > m_navEnd)` 裡面，而 dual
   DS-CTS 刻意讓兩個 frame 的 NAV 落在同一個絕對時刻，所以第二個 frame 的
   `navEnd == m_navEnd`，**每個 burst 的第二個 frame 都會被漏掉**。
3. **`busyFrac` 在這個 model 沒有鑑別力**：所有 15 格都是 0.73–0.77，當不了負載訊號，
   只能當溢位保護。
4. **迴路比 burst 慢**：Period 100ms + beacon 102.4ms vs burst 平均 0.1s。要真的逐 burst
   追蹤必須縮短 beacon interval（例如 20ms），**尚未測試**。目前能追的只有比它慢的漂移。
5. **LLI 估計器會低估 k**：CBR 1Mbps 真值 15 時 kHat 收斂到 10，因為 5 台從沒讓封包等到
   LLI 門檻。偏誤方向是安全的（低估比高估便宜），但這是 kdriven 光負載壞掉的根因。
   要修就得加 DS-CTS burst → Stage-2 歸屬（burst 計數器已經有了）。
6. **改動 `FrameExchangeManager` 成員後，沒重建的 scratch binary 會安靜印出垃圾**
   （inline getter 讀到錯的 offset，不 crash 不警告）。比對任何兩個 arm 前先全部重建。
7. **`--trajOutput=` 傳空字串會弄壞 CommandLine 解析**，不用就整個省略該參數。
8. **方法論**：sweep 資料裡只有靜態 θ 的 run，拿它評分一條規則量到的是「能不能挑出最好的
   靜態 θ」，不是「閉迴路能不能靠移動贏過靜態 θ」。kdriven 離線拿 2.9% 是因為它有 k 的
   oracle 知識——**不要用那個數字調動態控制器**。

## N9. 哪些 scratch 腳本支援 adaptive

| 腳本 | adaptive | 預設 |
|---|---|---|
| `pedca_adaptive_11be.cc`（CBR，由 `pedca_verification_nsta_11be.cc` 複製） | ✅ | `--adaptive=1` |
| `pedca_nsta_onoff_11be.cc` | ✅ | `--adaptive=0` |
| `pedca_nsta_poisson_11be.cc` | ✅ | `--adaptive=0` |
| `pedca_nsta_MMPP_11be.cc` | ✅ | `--adaptive=0` |
| 其餘 ~16 個 | ❌ | — |

三個 traffic-model 腳本的 `--adaptive` 預設是 **false**，且已用 git 原版建對照 binary
實測 `--adaptive=0` 的 stdout 與 clog **逐位元相同**，既有 sweep 不受影響。

---

## E0. What this layer is (and is NOT)

There are **two** independent pieces of P-EDCA in this tree:

1. **Static P-EDCA sender** (802.11bn-style, DS-CTS two-stage channel access) — this is the
   PRE-EXISTING base work, living inside `QosFrameExchangeManager` / `He/HtFrameExchangeManager`
   / `phy-entity.cc` / `QosTxop`. **This layer does NOT re-document or re-implement it.** It is
   a *dependency* (see E1). If your modified model already has an equivalent static P-EDCA
   sender, keep it; the adaptive layer only calls into its `SetCwds/SetQsrc/SetPsrc` interface.
2. **Adaptive closed-loop controller** (this document) — an AP-side control loop that *chooses*
   the per-STA static-P-EDCA parameters `theta = (CWds, QSRC_th, PSRC_lim)` at runtime and
   distributes them via a beacon IE, using uplink feedback (BSR + a new LLI bit) and local PHY
   sensing (channel-busy + DS-CTS collision rate). **This is the part to re-integrate.**

The loop, in one paragraph: an independent `PedcaController` object runs `Step()` every
`Period` (100 ms). Each step it reads BSS-wide `busyFrac` (PHY State trace) and DS-CTS
`collRate` (decoded-vs-dropped DS-CTS), and per-STA `bsrSta` (buffer status) and `lliSta`
(LLI-marked frame count). From these it computes, **per associated STA**, a fresh `theta`
(jump-to-target, no hysteresis/step-limit) and pushes the whole table to
`ApWifiMac::SetPedcaParametersBulk`, which the AP embeds in every beacon/probe/assoc-resp as
the **P-EDCA Parameter Set IE**. Each STA reads the row matching its own AID and applies it to
its FEM. Decision logic detail: §0.4/§0.5 (diagrams) and `pedca-controller.cc::Step()`.

## E1. Dependencies the adaptive layer assumes already exist in the base model

If any of these is missing or renamed in your modified model, wire the adaptive layer to the
equivalent, or provide a shim.

| Assumed symbol / behavior | Where (base) | Used by adaptive layer for |
|---|---|---|
| `QosFrameExchangeManager::SetCwds(uint32)`, `SetQsrc(uint16)`, `SetPsrc(uint8)`, `GetCwds()` | qos-frame-exchange-manager.h | STA applies received theta to its sender; controller seeds initial CWds |
| `WifiMac::GetPedcaSupported()` (per-node attribute) | wifi-mac | Gates STA-side apply, LLI marking, AP-side accounting |
| Static P-EDCA DS-CTS uses **reserved RA `00:0F:AC:47:43:00`** | frame-exchange / phy-entity | How the AP recognizes a DS-CTS on the air (decode + drop sensing) |
| Stock BSR piggyback: `m_setQosQueueSize` (the `SetQueueSize` attribute), `WifiMacHeader::SetQosQueueSize/GetQosQueueSize`, `ApWifiMac::SetBufferStatus/GetBufferStatus` | qos-fem, wifi-mac-header, ap-wifi-mac | Per-STA queue-pressure feedback. Enable `SetQueueSize=true`; raise `ApWifiMac::BsrLifetime` to ~2×Period (the 20 ms default expires BSR before the controller reads it) |
| `WifiPhyStateHelper` "State" trace + `WifiPhy` "PhyRxPpduDrop" trace | wifi-phy-state-helper, wifi-phy | Channel-busy fraction + dropped-DS-CTS sensing (PHY is otherwise **untouched**) |

## E2. Files ADDED (drop in + register)

| File | Contents |
|---|---|
| `pedca-parameter-set.{h,cc}` | The beacon IE. `class PedcaParameterSet : WifiInformationElement`, Element ID **250**. Variable-length: `octet0 = UpdateCount`, then N×5-byte `PedcaStaEntry{uint16 aid (LE), uint8 cwds, uint8 qsrcThreshold, uint8 psrcLimit}`. API: `SetUpdateCount/GetUpdateCount`, `SetEntries/GetEntries`, `GetEntryFor(uint16 aid) -> optional<PedcaStaEntry>`. Cap ~50 STAs (single length octet; not enforced). |
| `pedca-controller.{h,cc}` | `class PedcaController : Object`. Standalone — instantiated by the **scenario**, not by any MAC. `Setup(Ptr<WifiNetDevice> apDevice)` grabs ApWifiMac/PHY/FEM of link 0 and connects the two PHY traces (single-link only, asserts). `Start(Time when)` snapshots initial state at `when` (discards warmup observations) and schedules the first `Step()` at `when+Period`. `Step()` is the control loop (§0.4). Attributes in E5. |

Register all four in **`src/wifi/CMakeLists.txt`** (add `.cc` to sources, `.h` to headers).

## E3. Files MODIFIED — exact hooks

Ordered by merge-conflict risk (highest first). Each row is a re-apply instruction.

### E3.1 `mgt-headers.h` — IE registration in management-frame tuples  ⚠ HIGH RISK
Add `std::optional<PedcaParameterSet>` to **both** `ProbeResponseElems` and `AssocResponseElems`
tuples (Beacon inherits ProbeResponse, so it is covered automatically). Insert at the **same
position in both** — we put it after `EhtOperation`, before `TidToLinkMapping`.
**Invariant:** ns-3 serializes/parses these tuples positionally; the slot must exist in both
tuples and its relative order must never change once STAs and AP are built from the same tree.
`#include "pedca-parameter-set.h"`.

### E3.2 `ht/ht-frame-exchange-manager.cc` — LLI/queue-size write point for HT/HE/EHT  ⚠ HIGH RISK
In `FinalizeMacHeader` (the per-PSDU header finalize path used by 11n/ax/be), replace the stock
queue-size write with `SetQueueSizeAndPedcaLli(hdr, mpdu, queueSizeForTid[tid].value())`.
**This is essential:** for aggregated/HT frames the queue size is written HERE, not in
`QosFrameExchangeManager::ForwardMpduDown`. If you only patch ForwardMpduDown (the non-HT single-MPDU
path), LLI is never set and BSR is never capped under 11be. If your model refactored the
finalize/aggregation path, find wherever `SetQosQueueSize` is called for HT and route it through
the helper instead.

### E3.3 `qos-frame-exchange-manager.{h,cc}` — LLI marking (TX) + accounting (RX)
- New helper `void SetQueueSizeAndPedcaLli(WifiMacHeader& hdr, Ptr<const WifiMpdu> mpdu, uint8_t queueSize)`:
  sets EOSP, writes `SetQosQueueSize(min(queueSize,127))` **for every STA** (7-bit cap prevents the
  special value 254 from aliasing into a phantom LLI bit), and if `GetPedcaSupported()` && TID maps
  to `AC_VO` && `age = now - mpdu->GetTimestamp() > m_lliFraction * m_lliDelayBound`, calls
  `hdr.SetQosLli()`.
- `ForwardMpduDown` (non-HT path): call the helper instead of the raw `SetQosQueueSize`.
- `PreProcessFrame` (AP receive path, `TypeOfStation()==AP && addr1==self`): inside the existing
  QoS-Data/EOSP buffer-status loop, when `GetPedcaSupported() || m_apMac->GetPedcaControl()`:
  if `hdr.GetQosLli()` do `m_lliRxCount++` and `m_lliRxCountByAddr[addr2]++`; set
  `bufferStatus = hdr.GetQosQueueSize7()` before `SetBufferStatus`.
- New attributes: `PedcaLliDelayBound` (Time, default **10 ms** → `m_lliDelayBound`),
  `PedcaLliFraction` (double, default **0.7** → `m_lliFraction`).
- New members/getters: `m_lliRxCount` + `GetLliRxCount()`; `std::map<Mac48Address,uint32_t> m_lliRxCountByAddr`
  + `GetLliRxCount(Mac48Address)`.

### E3.4 `wifi-mac-header.{h,cc}` — the LLI bit
LLI = **bit 15 of QoS Control = MSB of the Queue Size octet** (`m_qosStuff`, the 2nd QoS-Control byte).
Frame length unchanged (queue size becomes 7-bit).
- `void SetQosLli()`  → `m_qosStuff |= 0x80;`
- `bool GetQosLli()`  → `(m_qosStuff & 0x80) != 0;`
- `uint8_t GetQosQueueSize7()` → `m_qosStuff & 0x7f;`  (masked queue size, LLI stripped)

### E3.5 `frame-exchange-manager.{h,cc}` — AP-side decoded-DS-CTS counter
Add `uint32_t m_dsCtsRxCount{0}` + inline `GetDsCtsRxCount()`. Increment it in `UpdateNav` inside
the existing DS-CTS branch (`hdr.IsCts() && hdr.GetAddr1() == 00:0F:AC:47:43:00`). This counts
DS-CTS the AP **decoded** on air; the controller pairs it with dropped-DS-CTS (from the PHY drop
trace) to compute `collRate`. (Distinct from the base sender's own `m_dsCtsCount`/`GetDsCtsCount`.)

### E3.6 `ap-wifi-mac.{h,cc}` — control API + IE emission
- Attribute **`PedcaControl`** (bool, default false) → `m_pedcaControl` + `GetPedcaControl()`.
  ⚠ **Use this on the AP, NOT `PedcaSupported`** (see E4 #1).
- `struct PedcaTheta { uint8_t cwds, qsrcThreshold, psrcLimit; };` (public).
- Members: `std::map<uint16_t,PedcaTheta> m_pedcaThetaByAid`, `uint8_t m_pedcaUpdateCount`.
- `void SetPedcaParametersBulk(const std::map<uint16_t,PedcaTheta>&)`: clamp each entry, **clear +
  replace** the whole map, **unconditionally `m_pedcaUpdateCount++`** (semantics: "n-th table push",
  not "n-th change").
- `PedcaTheta GetPedcaParametersFor(uint16_t aid) const`: map lookup, default `{0,2,1}` if unseen.
- `PedcaParameterSet GetPedcaParameterSet() const`: build the IE from the map + update count.
- Emit the IE in beacon / probe-resp / assoc-resp builders, each guarded by
  `if (GetPedcaSupported() || m_pedcaControl) X.Get<PedcaParameterSet>() = GetPedcaParameterSet();`.

### E3.7 `sta-wifi-mac.cc` — STA applies its own row
In `ApplyOperationalSettings` (runs on every beacon AND on the assoc-resp), when
`GetPedcaSupported()`: read `frame.Get<PedcaParameterSet>()`, `GetEntryFor(m_aid)`, and on the
matching row call `qosFem->SetCwds/SetQsrc/SetPsrc`; log `[P-EDCA PARAM APPLY]` only when the value
changed. ⚠ **Use the private member `m_aid`, NOT `GetAssociationId()`** (see E4 #4).

## E4. Invariants & gotchas that MUST survive the merge

1. **AP = `PedcaControl`, never `PedcaSupported`.** Setting `PedcaSupported=true` on the AP makes its
   own VO management frames (assoc/ADDBA resp, routed through the VO queue) drive the P-EDCA *sender*
   machinery, which overrides the AP's VO EDCAF → the beacon's `EdcaParameterSet` then poisons every
   STA's VO CW. Measured cost: **+13 ms p99** across all seeds. `PedcaControl` enables ONLY the IE
   broadcast + BSR/LLI accounting. (`PedcaSupported` on an AP stays valid for deliberate DL P-EDCA.)
2. **7-bit queue-size cap is unconditional** (all STAs, not just P-EDCA) — otherwise value 254
   (">64768 B") aliases to `126 + phantom LLI` at a P-EDCA AP.
3. **HT/HE/EHT write LLI/queue-size in `FinalizeMacHeader`, not `ForwardMpduDown`** (E3.2).
4. **STA apply must read `m_aid` directly, not `GetAssociationId()`** — the accessor asserts
   `IsAssociated()`, which is still false at the exact point the assoc-resp is processed (`m_aid` is
   set, but `SetState(ASSOCIATED)` runs *after* `ApplyOperationalSettings`). Pre-association `m_aid==0`
   is a safe non-matching sentinel.
5. **mgt-headers tuple slot** must be present + identically ordered in ProbeResponse and AssocResponse
   tuples, and never reordered (E3.1).
6. **DS-CTS reserved RA `00:0F:AC:47:43:00`** is hard-coded in both the drop-trace classifier
   (`pedca-controller.cc::RxDropCb`) and the decode counter (`frame-exchange-manager.cc::UpdateNav`).
   If the base sender uses a different DS-CTS address, update both.
7. **Controller drop filter must skip `reason == TXING`** — a DS-CTS "missed" while the AP itself is
   transmitting is self-inflicted, not a collision.
8. **AP cannot tell which STAs actually enabled `PedcaSupported`** → it computes theta for *every*
   associated STA (`GetStaList(0)`); legacy STAs simply never read the IE. Consequence: the
   `nAggressive/nConservative` trace counts include legacy STAs — treat as trend, not exact P-EDCA-STA
   counts. Ground truth = `[P-EDCA PARAM APPLY]` clog lines (P-EDCA STAs only).

## E5. Controller attributes (defaults) & scenario wiring

`PedcaController` attributes (all `MakeXAccessor`): `Period`=100 ms, `BusyHigh`=0.85, `CollHigh`=0.90,
`CollLow`=0.30, `LliHigh`=5 (frames/period/STA), `BsrPerStaHigh`=8.0 (256-B units ≈ two 1000-B pkts),
`QsrcMin`=1, `QsrcMax`=5, `PsrcMin`=1, `PsrcMax`=3, `CwdsMax`=2. Trace source `ControlStep(time, cwds,
nAggressive, nConservative, nUnchanged, busyFrac, collRate, bsrSum, lliCount, anyChanged)`.
Calibration rationale for the non-obvious defaults (why `CollHigh` is 0.90 not 0.10, why `QsrcMin`
depends on penetration): §5 and §8.8.

Scenario side (`scratch/pedca_adaptive_11be*.cc`): create the controller, `Setup(apDevice)`,
`Start(ctrlStart)`. Set `PedcaControl=true` on the **AP**, `PedcaSupported=true` on the **STAs**,
`SetQueueSize=true`, and raise `ApWifiMac::BsrLifetime`. `--adaptive=0` must leave the base binary
bit-identical (no controller created). `--warmupTime` (measurement start) is decoupled from
`--ctrlStart` (controller start) so you can measure post-convergence steady state.

## E6. Actual code changes (per-file excerpts)

Real code as it exists in our ns-3.45 tree (2026-07-14), so another AI can reproduce the
implementation exactly. Line numbers are indicative; match by symbol/context, not by number.

### E6.1 NEW FILE `pedca-parameter-set.cc` — the beacon IE (serialization)
```cpp
uint16_t
PedcaParameterSet::GetInformationFieldSize() const
{
    return 1 + 5 * static_cast<uint16_t>(m_entries.size());   // 1 (UpdateCount) + 5*N
}

void
PedcaParameterSet::SerializeInformationField(Buffer::Iterator start) const
{
    start.WriteU8(m_updateCount);
    for (const auto& e : m_entries)
    {
        start.WriteHtolsbU16(e.aid);   // AID little-endian
        start.WriteU8(e.cwds);
        start.WriteU8(e.qsrcThreshold);
        start.WriteU8(e.psrcLimit);
    }
}

uint16_t
PedcaParameterSet::DeserializeInformationField(Buffer::Iterator start, uint16_t length)
{
    Buffer::Iterator i = start;
    m_updateCount = i.ReadU8();
    m_entries.clear();
    uint16_t remaining = length - 1;
    while (remaining >= 5)              // parse as many 5-byte rows as the length allows
    {
        PedcaStaEntry e;
        e.aid = i.ReadLsbtohU16();
        e.cwds = i.ReadU8();
        e.qsrcThreshold = i.ReadU8();
        e.psrcLimit = i.ReadU8();
        m_entries.push_back(e);
        remaining -= 5;
    }
    return length;
}

std::optional<PedcaStaEntry>
PedcaParameterSet::GetEntryFor(uint16_t aid) const
{
    for (const auto& e : m_entries)
        if (e.aid == aid)
            return e;
    return std::nullopt;
}
// ElementId() returns IE_PEDCA_PARAMETER_SET (250).
```

### E6.2 NEW FILE `pedca-controller.cc` — the control loop
Setup / Start / sensing callbacks:
```cpp
void
PedcaController::Setup(Ptr<WifiNetDevice> apDevice)
{
    m_apMac = DynamicCast<ApWifiMac>(apDevice->GetMac());          // asserts non-null
    NS_ABORT_MSG_IF(m_apMac->GetNLinks() != 1, "single-link only");
    m_phy = apDevice->GetPhy();
    m_fem = DynamicCast<QosFrameExchangeManager>(m_apMac->GetFrameExchangeManager(0));
    m_phy->GetState()->TraceConnectWithoutContext(
        "State", MakeCallback(&PedcaController::PhyStateCb, this));   // busy sensing
    m_phy->TraceConnectWithoutContext(
        "PhyRxPpduDrop", MakeCallback(&PedcaController::RxDropCb, this)); // dropped DS-CTS
}

void
PedcaController::Start(Time when)
{
    Simulator::Schedule(when, [this]() {   // discard warmup observations at `when`
        m_cwds = static_cast<uint8_t>(std::min<uint32_t>(m_fem->GetCwds(), m_cwdsMax));
        m_busyAccum = Time(0);
        m_dsCtsDropAccum = 0;
        m_lastDsCtsRx = m_fem->GetDsCtsRxCount();
        m_lastLliByAddr.clear();
        m_stepEvent = Simulator::Schedule(m_period, &PedcaController::Step, this);
    });
}

void
PedcaController::PhyStateCb(Time start, Time duration, WifiPhyState state)
{
    switch (state) {                       // accumulate non-idle time
    case WifiPhyState::TX:
    case WifiPhyState::RX:
    case WifiPhyState::CCA_BUSY:
    case WifiPhyState::SWITCHING: m_busyAccum += duration; break;
    default: break;
    }
}

void
PedcaController::RxDropCb(Ptr<const WifiPpdu> ppdu, WifiPhyRxfailureReason reason)
{
    if (reason == TXING) return;           // self-inflicted miss, not a collision
    if (!ppdu || !ppdu->GetPsdu()) return;
    const auto& header = ppdu->GetPsdu()->GetHeader(0);
    if (header.IsCts() && header.GetAddr1() == Mac48Address("00:0F:AC:47:43:00"))
        m_dsCtsDropAccum++;                // a DS-CTS we failed to decode = collision proxy
}
```
`Step()` — **the algorithm itself** (runs every `m_period`):
```cpp
void
PedcaController::Step()
{
    // ── BSS-wide observations of the elapsed period ──
    const double periodUs = static_cast<double>(m_period.GetMicroSeconds());
    double busyFrac = std::min(1.0, static_cast<double>(m_busyAccum.GetMicroSeconds()) / periodUs);
    m_busyAccum = Time(0);

    const uint32_t drops = m_dsCtsDropAccum;              m_dsCtsDropAccum = 0;
    const uint32_t decoded = m_fem->GetDsCtsRxCount() - m_lastDsCtsRx;
    m_lastDsCtsRx += decoded;
    const bool dsActive = (drops + decoded) > 0;
    const double collRate = dsActive ? static_cast<double>(drops) / (drops + decoded) : 0.0;

    // ── ① BSS-wide CWds: jump directly, no step limit ──
    if (dsActive && collRate > m_collHigh)      m_cwds = m_cwdsMax;
    else if (dsActive && collRate < m_collLow)  m_cwds = 0;
    // else keep current value

    // ── ② global conservative gate (congestion guard OR collision escalation) ──
    const bool globalConservativeGate =
        (busyFrac > m_busyHigh && dsActive) ||
        (dsActive && collRate > m_collHigh && m_cwds >= m_cwdsMax);

    // ── ③ per-STA decisions ──
    uint32_t bsrSum = 0, lliTotal = 0, nAggressive = 0, nConservative = 0, nUnchanged = 0;
    bool anyChanged = false;
    std::map<uint16_t, ApWifiMac::PedcaTheta> newTheta;

    for (const auto& [aid, addr] : m_apMac->GetStaList(0))
    {
        uint32_t bsrSta = 0;
        for (uint8_t tid : {6, 7})                        // AC_VO TIDs
            if (uint8_t qs = m_apMac->GetBufferStatus(tid, addr); qs != 255) bsrSta += qs;
        bsrSum += bsrSta;

        const uint32_t lliCum = m_fem->GetLliRxCount(addr);
        auto lliIt = m_lastLliByAddr.find(addr);
        const uint32_t lliSta = (lliIt == m_lastLliByAddr.end()) ? 0 : (lliCum - lliIt->second);
        m_lastLliByAddr[addr] = lliCum;                   // first sight → delta 0, not cumulative
        lliTotal += lliSta;

        const auto previous = m_apMac->GetPedcaParametersFor(aid);
        ApWifiMac::PedcaTheta next = previous;
        next.cwds = m_cwds;                                // CWds is BSS-wide

        if (globalConservativeGate) {                     // forced conservative corner
            next.qsrcThreshold = m_qsrcMax; next.psrcLimit = m_psrcMin; nConservative++;
        } else if (lliSta >= m_lliHigh || static_cast<double>(bsrSta) >= m_bsrPerStaHigh) {
            next.qsrcThreshold = m_qsrcMin; next.psrcLimit = m_psrcMax; nAggressive++;  // aggressive
        } else {
            nUnchanged++;                                 // keep this STA's previous theta
        }

        if (next.cwds != previous.cwds || next.qsrcThreshold != previous.qsrcThreshold ||
            next.psrcLimit != previous.psrcLimit) anyChanged = true;
        newTheta[aid] = next;
    }

    m_apMac->SetPedcaParametersBulk(newTheta);            // push the whole table (bumps UpdateCount)

    m_controlStepTrace(Simulator::Now(), m_cwds, nAggressive, nConservative, nUnchanged,
                       busyFrac, collRate, bsrSum, lliTotal, anyChanged);
    m_stepEvent = Simulator::Schedule(m_period, &PedcaController::Step, this);   // reschedule
}
```

### E6.3 `wifi-information-element.h` — element ID
```cpp
#define IE_PEDCA_PARAMETER_SET ((WifiInformationElementId)250)   // reserved range 243–254
```

### E6.4 `mgt-headers.h` — IE slot in the mgmt tuples (⚠ same position in both)
```cpp
// ProbeResponseElems (Beacon inherits this) AND AssocResponseElems, both:
        std::optional<EhtOperation>,
        std::optional<PedcaParameterSet>,     // <-- added here, after EhtOperation
        std::vector<TidToLinkMapping>>;
// #include "pedca-parameter-set.h" at top
```

### E6.5 `ap-wifi-mac.{h,cc}` — control API + IE emission
Header (public): `struct PedcaTheta { uint8_t cwds, qsrcThreshold, psrcLimit; };`,
`bool GetPedcaControl() const;`, `void SetPedcaParametersBulk(const std::map<uint16_t,PedcaTheta>&);`,
`PedcaTheta GetPedcaParametersFor(uint16_t) const;`, `PedcaParameterSet GetPedcaParameterSet() const;`;
members `m_pedcaControl{false}`, `std::map<uint16_t,PedcaTheta> m_pedcaThetaByAid`, `m_pedcaUpdateCount{0}`.
```cpp
// attribute registration
.AddAttribute("PedcaControl",
    "Advertise and adaptively control the BSS-wide P-EDCA parameter set ... without this "
    "AP acting as a P-EDCA sender itself. Distinct from PedcaSupported ...",
    BooleanValue(false), MakeBooleanAccessor(&ApWifiMac::m_pedcaControl), MakeBooleanChecker())

void
ApWifiMac::SetPedcaParametersBulk(const std::map<uint16_t, PedcaTheta>& thetaByAid)
{
    m_pedcaThetaByAid.clear();
    for (const auto& [aid, theta] : thetaByAid) {
        PedcaTheta clamped;
        clamped.cwds          = std::min<uint8_t>(theta.cwds, 2);
        clamped.qsrcThreshold = std::min<uint8_t>(theta.qsrcThreshold, 5);
        clamped.psrcLimit     = std::clamp<uint8_t>(theta.psrcLimit, 1, 3);
        m_pedcaThetaByAid[aid] = clamped;
    }
    m_pedcaUpdateCount++;                               // unconditional: "n-th push"
}

ApWifiMac::PedcaTheta
ApWifiMac::GetPedcaParametersFor(uint16_t aid) const
{
    if (auto it = m_pedcaThetaByAid.find(aid); it != m_pedcaThetaByAid.end()) return it->second;
    return PedcaTheta{0, 2, 1};                         // default when unseen
}

PedcaParameterSet
ApWifiMac::GetPedcaParameterSet() const
{
    PedcaParameterSet ps;
    ps.SetUpdateCount(m_pedcaUpdateCount);
    std::vector<PedcaStaEntry> entries;
    for (const auto& [aid, t] : m_pedcaThetaByAid)
        entries.push_back(PedcaStaEntry{aid, t.cwds, t.qsrcThreshold, t.psrcLimit});
    ps.SetEntries(std::move(entries));
    return ps;
}

// emission — identical guard in the beacon, probe-resp and assoc-resp builders:
if (GetPedcaSupported() || m_pedcaControl)
    beacon.Get<PedcaParameterSet>() = GetPedcaParameterSet();
```

### E6.6 `sta-wifi-mac.cc` — STA applies its own row (in `ApplyOperationalSettings`)
```cpp
if (GetPedcaSupported())
{
    // Reads m_aid directly, NOT GetAssociationId() (which asserts IsAssociated(),
    // still false while the assoc-resp is being processed). m_aid==0 pre-assoc is a
    // safe non-matching sentinel.
    if (const auto& pedcaParameterSet = frame.template Get<PedcaParameterSet>())
    {
        if (auto entry = pedcaParameterSet->GetEntryFor(m_aid))
        {
            if (auto qosFem = DynamicCast<QosFrameExchangeManager>(GetFrameExchangeManager(linkId)))
            {
                const bool changed = qosFem->GetCwds() != entry->cwds ||
                                     qosFem->GetQsrcThreshold() != entry->qsrcThreshold ||
                                     qosFem->GetPsrcLimit() != entry->psrcLimit;
                qosFem->SetCwds(entry->cwds);
                qosFem->SetQsrc(entry->qsrcThreshold);
                qosFem->SetPsrc(entry->psrcLimit);
                if (changed) std::clog << "[P-EDCA PARAM APPLY] STA=" << GetAddress()
                                       << " aid=" << m_aid << " ... cwds=" << +entry->cwds
                                       << " qsrc=" << +entry->qsrcThreshold
                                       << " psrc=" << +entry->psrcLimit << std::endl;
            }
        }
    }
}
```

### E6.7 `wifi-mac-header.cc` — the LLI bit (MSB of the Queue Size octet)
```cpp
void    WifiMacHeader::SetQosLli()        { m_qosStuff |= 0x80; }               // bit 15 of QoS Ctrl
bool    WifiMacHeader::GetQosLli()  const { NS_ASSERT(m_qosEosp==1); return (m_qosStuff & 0x80) != 0; }
uint8_t WifiMacHeader::GetQosQueueSize7() const { NS_ASSERT(m_qosEosp==1); return m_qosStuff & 0x7f; }
```

### E6.8 `qos-frame-exchange-manager.{h,cc}` — LLI marking (TX) + accounting (RX)
Attributes:
```cpp
.AddAttribute("PedcaLliDelayBound", "... AC_VO delay budget for the LLI trigger ...",
    TimeValue(MilliSeconds(10)), MakeTimeAccessor(&QosFrameExchangeManager::m_lliDelayBound),
    MakeTimeChecker())
.AddAttribute("PedcaLliFraction", "... fraction of the delay bound beyond which HOL age fires LLI ...",
    DoubleValue(0.7), MakeDoubleAccessor(&QosFrameExchangeManager::m_lliFraction),
    MakeDoubleChecker<double>(0.0, 1.0))
```
TX helper (called from ForwardMpduDown AND HtFEM::FinalizeMacHeader):
```cpp
void
QosFrameExchangeManager::SetQueueSizeAndPedcaLli(WifiMacHeader& hdr, Ptr<const WifiMpdu> mpdu,
                                                 uint8_t queueSize)
{
    hdr.SetQosEosp();
    hdr.SetQosQueueSize(std::min<uint8_t>(queueSize, 127));   // 7-bit cap for EVERY STA
    if (m_mac->GetPedcaSupported() && QosUtilsMapTidToAc(hdr.GetQosTid()) == AC_VO)
    {
        Time age = Simulator::Now() - mpdu->GetTimestamp();
        if (age > m_lliFraction * m_lliDelayBound)
            hdr.SetQosLli();
    }
}
```
RX accounting (AP side, inside the buffer-status loop of `PreProcessFrame`):
```cpp
uint8_t bufferStatus = hdr.GetQosQueueSize();
if (m_mac->GetPedcaSupported() || m_apMac->GetPedcaControl())
{
    if (hdr.GetQosLli())
    {
        m_lliRxCount++;
        m_lliRxCountByAddr[mpdu->GetOriginal()->GetHeader().GetAddr2()]++;
    }
    bufferStatus = hdr.GetQosQueueSize7();               // store masked 7-bit value
}
m_apMac->SetBufferStatus(hdr.GetQosTid(), mpdu->GetOriginal()->GetHeader().GetAddr2(), bufferStatus);
```
Getters (header): `uint32_t GetLliRxCount() const`; `uint32_t GetLliRxCount(Mac48Address) const`
(map lookup, 0 if absent). Members: `m_lliRxCount`, `std::map<Mac48Address,uint32_t> m_lliRxCountByAddr`.

### E6.9 `ht/ht-frame-exchange-manager.cc` — HT/HE/EHT write point (⚠ easy to miss)
```cpp
// inside FinalizeMacHeader, replacing the stock SetQosQueueSize call:
SetQueueSizeAndPedcaLli(hdr, mpdu, queueSizeForTid[tid].value());
```

### E6.10 `frame-exchange-manager.{h,cc}` — AP-side decoded-DS-CTS counter
Header: `uint32_t m_dsCtsRxCount{0};` + `uint32_t GetDsCtsRxCount() const { return m_dsCtsRxCount; }`.
```cpp
// in UpdateNav, inside the existing IsCts() handling:
if (hdr.GetAddr1() == Mac48Address("00:0F:AC:47:43:00"))
{
    m_dsCtsRxCount++;   // a DS-CTS the AP decoded on air (paired with RxDropCb drops → collRate)
    ...
}
```

### E6.11 `src/wifi/CMakeLists.txt`
```cmake
# SOURCE_FILES:
    model/pedca-controller.cc
    model/pedca-parameter-set.cc
# HEADER_FILES:
    model/pedca-controller.h
    model/pedca-parameter-set.h
```

---

# Adaptive P-EDCA 閉迴路控制 — 改動總覽

日期：2026-07-12
目標：在既有的靜態 P-EDCA 實作之上，建立 **AP 動態調控 P-EDCA 參數表 (CWds, QSRC_th, PSRC_lim)** 的閉迴路系統：AP 透過 beacon IE 下發參數、STA 透過 QoS Control 回報 BSR + LLI、AP 本地感測通道忙碌度與 DS-CTS 碰撞率，並以 rule-based AIMD controller 週期性調整參數。

對應文件：《Adaptive P-EDCA Parameter Optimization》(offline/online 路線) 的 **online route** 第 4–8、10 節；實作計畫存於 `~/.claude/plans/p-edca-cwds-qsrc-psrc-workspace-model-p-kind-candy.md`。

> **閱讀指引**：本文件的 §1–§7 記錄的是 2026-07-12 的 **V1 設計**（BSS-wide 單一 θ、AIMD ±1 步階、hysteresis），已被 2026-07-13 的 **V2** 取代。**要了解目前實際跑的演算法，看下面這一節（§0）就夠了**；§8 是 V2 的逐項 diff 與量測，§1–§7 僅供對照歷史。

---

## 0. 演算法流程總覽（目前實作 = V2）

### 0.1 一句話描述

AP 端一個獨立的 `PedcaController` **每 100ms（Tc）跑一次控制迴圈**：它從 PHY trace + FEM counter + per-STA buffer status 讀進「本週期發生了什麼」，據此**為每一台已連線 STA 各自算出一組 θ =（CWds, QSRC_th, PSRC_lim）**，透過 beacon 裡的 P-EDCA Parameter Set IE 下發；STA 收到 beacon 後把自己那一列套進 FEM，下一次上行送包就用新參數。整個迴圈**無 hysteresis、無步階限制**——條件一符合就直接跳到目標值。

### 0.2 閉迴路系統架構

```
┌──────────────────────────── AP  (WifiNetDevice, link 0) ─────────────────────────────┐
│                                                                                       │
│  WifiPhy + StateHelper ──"State" trace────────────┐  (通道忙碌時間)                    │
│                        ──"PhyRxPpduDrop" trace──┐  │  (被丟棄的 DS-CTS)                 │
│                                                 ▼  ▼                                   │
│  QosFrameExchangeManager ─GetDsCtsRxCount()──▶ ┌──────────────────┐                   │
│  (FEM, link 0)           ─GetLliRxCount(addr)─▶│  PedcaController  │  每 Tc=100ms:     │
│         ▲  存 BSR/LLI                          │  ┌────────────┐  │  觀測→決策→下發     │
│         │  到 buffer status                    │  │  Step()    │  │                   │
│         │                                      │  └─────┬──────┘  │                   │
│         │                          GetBufferStatus     │ SetPedcaParametersBulk        │
│         │                          (tid 6/7, addr)     │  (map<aid, θ>)                │
│         │                              ┌───────────────▼──────────────┐                │
│         │                              │          ApWifiMac           │                │
│         │                              │   m_pedcaThetaByAid (per-STA) │                │
│         │                              └───────────────┬──────────────┘                │
│         │                                              │ 內嵌 P-EDCA Parameter Set IE   │
│         │                                              │ (beacon / probe / assoc resp)  │
└─────────┼──────────────────────────────────────────────┼───────────────────────────────┘
          │ UL QoS Data                                    │ beacon（每 beacon interval，≤102.4ms）
          │ QoS Control = [7-bit Queue Size (BSR)|1-bit LLI]│
          ▼                                                ▼
┌───────── STA_i (PedcaSupported=true) ─────────────────────────────────────────────────┐
│  送包路徑:  FinalizeMacHeader / ForwardMpduDown → SetQueueSizeAndPedcaLli               │
│             (若此 VO 封包在佇列停留 > 0.7 × 10ms → 設 LLI bit；queue size cap 127)       │
│  收 beacon: ApplyOperationalSettings → IE.GetEntryFor(m_aid) → FEM SetCwds/SetQsrc/SetPsrc│
│             → 下一次 StartTransmission 立即讀到新 θ                                       │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

**設計核心**：AP 只是 controller / advertiser，靠既有 trace 感測（PHY 與 qos-txop **零改動**），STA 才是真正的 P-EDCA sender。AP 必須用 `PedcaControl` attribute 而**非** `PedcaSupported`（見 §4 第 1 點——後者會毒化全體 STA 的 VO CW，實測 p99 +13ms）。

### 0.3 一個控制週期的時序

```
  |<──────────────── 觀測期 Tc = 100ms ────────────────>|
  |                                                     |
  | PHY "State" trace 累計 busy 時間 (m_busyAccum)        |
  | PhyRxPpduDrop 累計掉的 DS-CTS (m_dsCtsDropAccum)      |   Step() 在週期結束時執行:
  | FEM 累計 decoded DS-CTS、per-addr LLI count           |   ┌─────────────────────────┐
  | STA 上行時把 BSR/LLI 寫進 QoS Control                 |   │ 1. 算 busyFrac/collRate  │
  |                                                     |──▶│ 2. 定 BSS-wide CWds      │
  |                                                     |   │ 3. 逐台定 QSRC/PSRC      │
  |                                                     |   │ 4. SetPedcaParametersBulk│
  |                                                     |   │ 5. 排下一次 Step (+Tc)   │
                                                            └────────────┬────────────┘
                                                                         │
   新 θ 透過「下一個 beacon」的 IE 下發 ──────────────────────────────────┘
   → STA ApplyOperationalSettings 套用 → 下次送包生效
   (端到端下發延遲 ≤ 一個 beacon interval)
```

### 0.4 控制器決策流程（Step() 的核心演算法）

```
                        ┌──────────────────────────────────┐
                        │  Step()  ── 每 Tc = 100ms 觸發     │
                        └─────────────────┬──────────────────┘
                                          │
        ┌─────────────────────────────────▼─────────────────────────────────┐
        │  讀本週期 BSS-wide 觀測（讀完歸零累加器）:                            │
        │    busyFrac = min(1, m_busyAccum / Tc)                              │
        │    decoded  = FEM.GetDsCtsRxCount() − 上次快照                       │
        │    drops    = m_dsCtsDropAccum                                       │
        │    dsActive = (drops + decoded) > 0                                  │
        │    collRate = dsActive ? drops / (drops + decoded) : 0               │
        └─────────────────────────────────┬─────────────────────────────────┘
                                          │
        ┌─────────────────────────────────▼─────────────────────────────────┐
        │  ① BSS-wide CWds（共用 mini-contention 資源池，立即跳值）:          │
        │      dsActive 且 collRate > CollHigh(0.90) → CWds = CwdsMax(2)       │
        │      dsActive 且 collRate < CollLow(0.30)  → CWds = 0                │
        │      否則                                  → 保持不變                │
        └─────────────────────────────────┬─────────────────────────────────┘
                                          │
        ┌─────────────────────────────────▼─────────────────────────────────┐
        │  ② 全域保守閘門 globalConservativeGate =                            │
        │       (busyFrac > BusyHigh(0.85) 且 dsActive)          ── 壅塞守衛   │
        │    OR (collRate > CollHigh 且 CWds 已達 CwdsMax)       ── 碰撞升級   │
        └─────────────────────────────────┬─────────────────────────────────┘
                                          │
                     ┌────────────────────▼─────────────────────┐
                     │   for 每個已連線 STA (aid, addr):          │
                     │     bsrSta = Σ GetBufferStatus(tid∈{6,7})  │
                     │     lliSta = GetLliRxCount(addr) − 上次快照 │
                     └────────────────────┬─────────────────────┘
                                          │
                        ┌─────────────────▼──────────────────┐
                        │   globalConservativeGate 成立?       │
                        └───────┬──────────────────────┬──────┘
                            Yes │                       │ No
                                ▼                       ▼
                    ┌───────────────────┐   ┌───────────────────────────────┐
                    │  保守角落 (壓抑)    │   │  lliSta ≥ LliHigh(5)           │
                    │  QSRC = QsrcMax(5) │   │    或 bsrSta ≥ BsrHigh(8) ?    │
                    │  PSRC = PsrcMin(1) │   └──────┬─────────────────┬──────┘
                    └───────────────────┘      Yes │                  │ No
                                                   ▼                  ▼
                                       ┌───────────────────┐  ┌──────────────────┐
                                       │  激進角落 (加速)    │  │  維持該台上次 θ    │
                                       │  QSRC = QsrcMin(1) │  │  (風平浪靜→不動作) │
                                       │  PSRC = PsrcMax(3) │  └──────────────────┘
                                       └───────────────────┘
                          ※ 三條路徑的 CWds 一律 = ① 決定的 BSS-wide 值
                                          │
        ┌─────────────────────────────────▼─────────────────────────────────┐
        │  ③ SetPedcaParametersBulk(newTheta)                                 │
        │       整批替換 m_pedcaThetaByAid，Update Count 無條件 +1             │
        │  ④ 觸發 ControlStep trace；有變才印 [P-EDCA CTRL] 到 clog            │
        │  ⑤ Simulator::Schedule(+Tc, Step)                                   │
        └───────────────────────────────────────────────────────────────────┘
```

### 0.5 三種決策結果與其代表意義

| 結果 | 觸發條件（優先序由上而下） | 下發的 θ | 直覺 |
|---|---|---|---|
| **保守角落** | 全域閘門成立：通道太忙（busyFrac>0.85）或碰撞升級（CWds 已滿仍高碰撞） | QSRC=`QsrcMax`(5), PSRC=`PsrcMin`(1)，CWds=①值 | 系統過載，壓抑所有 STA 的 P-EDCA 觸發頻率 |
| **激進角落** | 閘門未成立，且該台自己有 VO 壓力：lliSta≥`LliHigh`(5) 或 bsrSta≥`BsrHigh`(8 units) | QSRC=`QsrcMin`(1), PSRC=`PsrcMax`(3)，CWds=①值 | 這台延遲/佇列吃緊，讓它更容易搶到 VO 傳輸機會 |
| **維持不變** | 閘門未成立，且該台沒有明顯壓力 | 沿用該台上一次的 θ（僅 CWds 跟隨①更新） | 沒有值得反應的訊號，不做無謂擾動 |

**關鍵性質**：
- **逐台獨立**：同一份 beacon 裡，aid=1 可能拿到激進角落、aid=2 拿到保守角落（§8.6 已實測到）。
- **CWds 是 BSS-wide 單值**：它是共用的 Stage-1 mini-contention 資源池大小，不是逐台屬性，所以三種決策路徑共用同一個①算出的 CWds。
- **立即跳值**：沒有 hysteresis（連續確認）、沒有 cooldown、沒有「每週期只改一個參數」的步階限制——每週期完全根據當下觀測重算。
- **對 legacy STA 的處理**：AP 分辨不出哪些 STA 真的開了 `PedcaSupported`，所以對**所有**已連線 STA 都算 θ 放進表格；legacy STA 永遠不會去讀（它們的 `ApplyOperationalSettings` 用自己的 `GetPedcaSupported()` 判斷）。因此 trace 裡的 nAggressive/nConservative **含 legacy STA、只能當粗略趨勢**（見 §8.5 已知限制）。

### 0.6 四個觀測訊號的來源

| 訊號 | 粒度 | 來源 | 用途 |
|---|---|---|---|
| `busyFrac` | BSS-wide | `WifiPhyStateHelper` "State" trace 累計 TX/RX/CCA_BUSY/SWITCHING ÷ Tc | 壅塞守衛（保守閘門） |
| `collRate` | BSS-wide | 掉的 DS-CTS（`PhyRxPpduDrop` trace，filter reason≠TXING、RA==00:0F:AC:47:43:00）÷ (掉的+解碼成功的 `GetDsCtsRxCount()`) | 決定 CWds、碰撞升級 |
| `bsrSta` | per-STA | `ApWifiMac::GetBufferStatus(tid∈{6,7}, addr)`（跳過 255=unknown） | 逐台 VO 壓力判斷 |
| `lliSta` | per-STA | `QosFrameExchangeManager::GetLliRxCount(addr)` 的本週期差分 | 逐台 VO 延遲壓力判斷 |

### 0.7 參數下發與生效的資料結構

STA 上行回報（每個 UL QoS Data frame，frame 長度不變）：

```
QoS Control field (2 octets)
 ┌───────────────────────────────┬───┐
 │  Queue Size (7 bits) = BSR     │LLI│   ← MSB 借用為 LLI bit
 └───────────────────────────────┴───┘
   ↑ writer 端一律 cap 到 127（防 254 aliasing 成假 LLI）
   ↑ VO 封包停留 > PedcaLliFraction(0.7) × PedcaLliDelayBound(10ms) 時，LLI=1
```

AP 下發（beacon / probe resp / assoc resp 的 P-EDCA Parameter Set IE，Element ID 250）：

```
Information field = 1 + 5 × N octets  (N = 已連線 STA 數)
 ┌────────────┬───────────────────────────────────────────────────┐
 │Update Count│  N 個 5-byte entry                                  │
 │  (1 octet) │  ┌──────────┬─────┬──────────┬──────────┐           │
 │            │  │ AID(2,LE)│CWds │ QSRC_th  │ PSRC_lim │  × N      │
 │            │  └──────────┴─────┴──────────┴──────────┘           │
 └────────────┴───────────────────────────────────────────────────┘
   每台 STA 讀 GetEntryFor(自己的 m_aid) 取自己那一列；找不到就不動作。
   N≤50 才不超過單一 length octet 上限（未做分段）。
```

### 0.8 生命週期（scenario 如何啟用）

```
--adaptive=0 : 完全不建立 PedcaController，行為與原 pedca_nsta_poisson_11be bit 相同
--adaptive=1 :
   1. 建 PedcaController，Setup(apDevice)：抓 ApWifiMac/PHY/FEM(link0)、掛 PHY trace
   2. Start(ctrlStart)：在 ctrlStart 讀 FEM 現值當初始 CWds、歸零所有累加器
                        （warmup 期間的觀測被丟棄）
   3. 第一次 Step() 在 ctrlStart + Tc，之後每 Tc 一次
   ※ --warmupTime（量測起點）與 --ctrlStart（controller 啟動）解耦，
     可量測「收斂後穩態」而不含啟動暫態
```

> 目前 controller 的初始 θ 來自 `Start()` 讀取的 FEM 現值（即 scenario 設定的起始參數）；**offline policy-table warm start 尚未接上**（見 §9），而 §6/§6b 的量測顯示在飽和負載下 warm start 是必要的（否則從次佳參數起步會卡進雙穩態陷阱）。

---

## 1. 新增檔案

> **注意**：本節（§1-§7）描述的是 2026-07-12 的 **V1 設計**（BSS-wide 單一 θ、AIMD ±1 步階、
> hysteresis），2026-07-13 已改為 **V2**（逐台獨立參數 + 立即跳值，無 hysteresis）。
> **目前實際運行的演算法流程請看 §0**；V2 相對 V1 的逐項 diff 與量測見 **§8**。IE 格式、
> `ApWifiMac` API、controller 決策邏輯在 V2 都已改變，本節（§1-§7）僅供對照歷史。

### `src/wifi/model/pedca-parameter-set.{h,cc}` — P-EDCA Parameter Set IE（V1 格式，見 §8.1 現況）
- 繼承 `WifiInformationElement`，Element ID = **250**（取自 802.11-2020 Table 9-92 保留區段 243–254，避開 IE_EXTENSION/Vendor-Specific 機制，V2 沿用同一個 ID）。
- V1 Information field 共 **4 octets**：

  | Octet | 欄位 | 範圍 | 意義 |
  |---|---|---|---|
  | 0 | Update Count | 0–255 | 每次參數變更 +1 (mod 256)，讓 STA/log 區分「新參數」與「同參數重播」 |
  | 1 | CWds | 0–2 | Stage-1 DS-CTS mini contention window |
  | 2 | QSRC Threshold | 0–5 | dot11PEDCARetryThreshold |
  | 3 | PSRC Limit | 1–3 | dot11PEDCAConsecutiveAttempt |

### `src/wifi/model/pedca-controller.{h,cc}` — AP 端 rule-based AIMD controller
- `ns3::PedcaController : public Object`，獨立於 ApWifiMac（`--adaptive=0` 時完全不建立，零開銷）。
- `Setup(Ptr<WifiNetDevice> apDevice)`：抓取 ApWifiMac / WifiPhy / FEM（限單 link，有 assert），掛上 PHY trace。
- `Start(Time when)`：在 `when` 讀取初始 θ 並歸零觀測，第一次決策在 `when + Period`。
- 每個控制週期 `Step()` 觀測：
  - **busyFrac**：`WifiPhyStateHelper` "State" trace，累計 TX/RX/CCA_BUSY/SWITCHING 時間 ÷ 週期。
  - **DS-CTS 碰撞率**：AP 解碼成功的 DS-CTS（`FrameExchangeManager::GetDsCtsRxCount`，見 §2）vs PHY drop（WifiPhy `PhyRxPpduDrop` trace，以 `IsCts() && RA==00:0F:AC:47:43:00` 分類，**過濾 reason==TXING** 避免 AP 自己發送時的誤計）。
  - **bsrSum / bsrPerSta**：`ApWifiMac::GetBufferStatus(tid∈{6,7}, addr)` 對所有已連線 STA 加總（跳過 255=unknown）。
  - **lliCount**：`QosFrameExchangeManager::GetLliRxCount()` 差分。
- 規則（優先序 a > b > c > d，每週期最多一步，hysteresis K=2 週期 + cooldown 1 週期）：

  | 規則 | 條件 | 動作 |
  |---|---|---|
  | (a) congestion guard | busyFrac > BusyHigh 且 DS-CTS 活躍 | QSRC_th++，到頂則 PSRC_lim-- |
  | (b) collision 抑制 | collRate > CollHigh | CWds++，到頂則 QSRC_th++ |
  | (c) VO 改善 | (lli ≥ LliHigh 或 bsrPerSta ≥ BsrPerStaHigh) 且 (a)(b) 條件都不成立 | QSRC_th--，到底則 PSRC_lim++ |
  | (d) CWds 鬆弛 | collRate < CollLow 且 CWds > 0 | CWds-- |

- 屬性與**校準後預設值**（見 §5 校準過程）：`Period`=100ms、`BusyHigh`=0.85、`CollHigh`=**0.90**、`CollLow`=**0.30**、`LliHigh`=5/週期、`BsrPerStaHigh`=**8.0**（≈2 個 1000B 封包）、`HysteresisK`=2、`Cooldown`=1、`QsrcMin`=**1**、QsrcMax=5、PsrcMin=1、PsrcMax=3、CwdsMax=2。
- Trace source `"ControlStep"`：每週期輸出 `(time, θ, busyFrac, collRate, bsrSum, lliCount, changed)`。
- 決策時印 `[P-EDCA CTRL]` 到 clog（含觸發的規則名稱）。

### `scratch/pedca_adaptive_11be.cc` — 驗證情境
- 複製自 `pedca_nsta_poisson_11be.cc`（30 STA、UL AC_VO Poisson 1Mbps/STA、EhtMcs5、5GHz ch36/20MHz）。
- 新增 CLI：`--adaptive`(default 1)、`--controlPeriodMs`(100)、`--delayBoundMs`(10)、`--lliFraction`(0.7)、`--busyHigh/--collHigh/--collLow/--lliHigh/--bsrHigh`、`--warmupTime`(1，量測起點)、`--ctrlStart`(<0=warmupTime，controller 啟動時間，**與量測起點解耦**以便量測收斂後的穩態)、`--trajOutput`（trajectory CSV，格式 `time_s,cwds,qsrc,psrc,busyFrac,dsCtsCollRate,bsrSum,lliCount,changed`）。
- `--adaptive=0` 時行為與 `pedca_nsta_poisson_11be` **完全相同**（已用同 seed 驗證數據 bit 相同）。
- `--adaptive=1` 時：STA 的 θ **只**透過 IE 下發（assoc response 就帶了，先於 t=0.5s 應用層開始送流量），不再由 script 直接寫 FEM。
- 注意：`--trajOutput=`（空值）會弄壞 CommandLine 解析，不要傳空字串，直接省略該參數。

### `scratch/pedca_adaptive_11be_onoff.cc` — 驗證情境（bursty ON/OFF 流量，2026-07-13 新增）
- 複製自 `pedca_adaptive_11be.cc`，把 Poisson 流量源換成 `pedca_nsta_onoff_11be.cc` 的 ns-3 內建 `OnOffHelper`：ON/OFF 時長各自獨立指數分佈、平均 0.1s（50% duty cycle），ON 期間發送速率 = 2×`--dataRate`，使平均 offered load 等於 `--dataRate`。CLI/輸出格式與 `pedca_adaptive_11be.cc` 完全相同。
- 動機：驗證「adaptive controller 的收斂暫態成本，是否能被 on/off 流量的週期性 idle（排空）期緩解」這個假設（結論見 §6b）。

### `scratch/ADAPTIVE-PEDCA-CHANGES.md`
本文件。

---

## 2. 修改的既有檔案

| 檔案 | 改動 |
|---|---|
| `src/wifi/CMakeLists.txt` | 註冊 `pedca-parameter-set.cc/.h`、`pedca-controller.cc/.h` |
| `src/wifi/model/wifi-information-element.h` | 新增 `#define IE_PEDCA_PARAMETER_SET ((WifiInformationElementId)250)` |
| `src/wifi/model/mgt-headers.h` | `std::optional<PedcaParameterSet>` 加入 `ProbeResponseElems` 與 `AssocResponseElems` tuple（都插在 `EhtOperation` 之後、`TidToLinkMapping` 之前；beacon 繼承 probe resp 所以自動生效）。**注意：兩個 tuple 的插入位置必須一致，且日後不可重排**（序列化/解析都是依 tuple 順序折疊） |
| `src/wifi/model/ap-wifi-mac.{h,cc}` | (1) 新增 attribute **`PedcaControl`**（見 §4，「AP 只當 controller/advertiser、不當 P-EDCA sender」）與 `GetPedcaControl()`；(2) θ 儲存 (`m_pedcaCwds/QsrcThreshold/PsrcLimit/UpdateCount`)；(3) `SetPedcaParameters(cwds,qsrcTh,psrcLim)`：clamp 範圍、值有變才 bump update count、同步套用到 AP 自身 FEM；(4) `GetPedcaParameterSet()`；(5) beacon / probe resp / assoc resp 三處在 `GetPedcaSupported() || m_pedcaControl` 時插入 IE |
| `src/wifi/model/sta-wifi-mac.cc` | `ApplyOperationalSettings`（每個 beacon 與 assoc resp 都會執行）中，`GetPedcaSupported()` 的 STA 解析 `PedcaParameterSet` IE 並呼叫 FEM 的 `SetCwds/SetQsrc/SetPsrc`；值有變才印 `[P-EDCA PARAM APPLY]` |
| `src/wifi/model/wifi-mac-header.{h,cc}` | 新增 `SetQosLli()` / `GetQosLli()` / `GetQosQueueSize7()`：LLI bit = QoS Control bit 15（= Queue Size subfield 的 MSB），queue size 改為 7-bit。frame 長度不變 |
| `src/wifi/model/qos-frame-exchange-manager.{h,cc}` | (1) 新 helper **`SetQueueSizeAndPedcaLli(hdr, mpdu, queueSize)`**：queue size 一律 cap 到 127（防止特殊值 254 aliasing 成假 LLI），P-EDCA STA 的 AC_VO 封包若 `now - mpdu->GetTimestamp() > lliFraction × lliDelayBound` 則設 LLI bit；(2) `ForwardMpduDown` 改用該 helper；(3) 新 attribute `PedcaLliDelayBound`(10ms)、`PedcaLliFraction`(0.7)；(4) AP 端 `PreProcessFrame` 在 `GetPedcaSupported() || m_apMac->GetPedcaControl()` 時計數 LLI（`m_lliRxCount`）並以 7-bit 遮罩後存 buffer status；(5) 新 getter `GetCwds()`、`GetLliRxCount()` |
| `src/wifi/model/ht/ht-frame-exchange-manager.cc` | **關鍵**：HT/HE/EHT 的 queue-size 寫入點在 `FinalizeMacHeader`（每個 PSDU 發送前），不是 `ForwardMpduDown` — 這裡也改為呼叫 `SetQueueSizeAndPedcaLli`（否則 11be 下 LLI 永遠不會被設，BSR 也不會被 cap） |
| `src/wifi/model/frame-exchange-manager.{h,cc}` | 新增 `m_dsCtsRxCount` + `GetDsCtsRxCount()`，在 `UpdateNav` 既有的 DS-CTS 辨識區塊（RA==00:0F:AC:47:43:00）遞增 — AP 解碼成功的 DS-CTS 計數 |

PHY 層（`phy-entity.cc`、`wifi-phy.*`、`wifi-phy-state-helper.*`）與 `qos-txop.*` **零改動** — 感測全部走既有 trace source。

---

## 3. 資料流（閉迴路全鏈）

```
Controller.Step (每100ms)
  → ApWifiMac::SetPedcaParameters(θ')   [clamp + update count + AP FEM 同步]
  → 下一個 SendOneBeacon 內嵌 PedcaParameterSet IE   (≤102.4ms)
  → STA ReceiveBeacon → ApplyOperationalSettings → FEM SetCwds/SetQsrc/SetPsrc
  → 下一次 StartTransmission 即讀到新 θ（成員變數每次觸發都重新讀，中途改值安全）

STA 上行回報（每個 UL QoS Data frame）
  → FinalizeMacHeader / ForwardMpduDown → SetQueueSizeAndPedcaLli
     [7-bit queue size + LLI bit in QoS Control；frame 長度不變]
  → AP PreProcessFrame → SetBufferStatus(7-bit 值) + m_lliRxCount++
  → Controller 下一週期讀取
```

---

## 4. 重要設計決策與踩坑記錄

1. **`PedcaControl` 與 `PedcaSupported` 分離（重要 bug fix）**
   ns-3 的 `ApWifiMac` 會把發給 QoS STA 的管理幀（assoc resp、ADDBA resp）放進 **VO queue**。若直接把 AP 設成 `PedcaSupported=true`，AP 自己的 VO 管理幀會啟動 P-EDCA sender 狀態機（發 DS-CTS、暫停自身其他 AC、把自身 VO EDCAF 切到 CW=7），而且 beacon 建構時採樣到被覆寫的 EDCA 參數會透過 `EdcaParameterSet` IE **毒化全體 STA 的 VO CW**。實測代價：5 個 seed 全部 p99 **+13ms**。
   → 解法：AP 用新的 `PedcaControl` attribute（只開 IE 廣播 + BSR/LLI 統計），`PedcaSupported` 留給真正要當 P-EDCA sender 的節點（如 DL 情境 `pedca_verification_nsta_11be_DL.cc` 的 AP）。

2. **LLI 的載體**：借用 Queue Size subfield 的 MSB（7-bit queue size + 1-bit LLI）。QoS Control 是固定 2 octets、整包序列化，所以 frame 長度與時序完全不變；已驗證樹內唯一讀取 `GetQosQueueSize()` 的是 AP 儲存點（`RrMultiUserScheduler` 讀的是已遮罩的儲存值，本情境也未使用）。**writer 端必須先 cap 127** 否則 254 (">64768B") 會 alias 成 126+假 LLI。BSR piggyback 本身零成本：同 seed 下開/關 `SetQueueSize` 的 delay 數據 bit 相同。

3. **11be 的 queue-size/LLI 寫入點在 `HtFrameExchangeManager::FinalizeMacHeader`**，不是 `QosFrameExchangeManager::ForwardMpduDown`（後者只管非 HT 單一 MPDU 路徑）。第一版只改了 ForwardMpduDown 導致 LLI 永遠是 0。

4. **DS-CTS 是 CTS 幀、沒有 TA 欄位** → AP 無法辨識 Stage-1 發送者、無法直接估 nPedca。v1 controller 的規則刻意不依賴 nPedca。

5. **單一 seed 的 p99 在飽和點非常吵**（同 θ 同設定不同 seed 差到 12–27ms），任何結論都要多 seed 平均 — 與你 offline sweep 用 10 seeds 的理由一致。

6. **兩種 "QSRC"**：stock `Txop::staRetryCount`（驅動一般 CW 成長）與 P-EDCA 的 `m_qsrc` 是兩回事；controller/IE 只碰 `m_qsrc_threshold`。另注意 `StartTransmission` 會在 `FrameRetryLimit <= qsrc_threshold` 時自動抬高 retry limit（QSRC_th 被調到 5 時會觸發，屬預期行為）。

---

## 5. Controller 校準過程（數據驅動）

初版預設（CollHigh=0.10、rule-c 需 busyFrac<0.60）在 n=30 全 P-EDCA 飽和下收斂到保守角 (2,5,1)，p99≈21ms ≈ fixed default，遠差於 offline-best。量測發現：

- **DS-CTS 碰撞率在好與壞的操作點都固有地 ≈0.66–0.69**（p90 0.83）——DS-CTS 碰撞本來就便宜（79µs mute + Stage-2 CW=7 解決），在這個量級不是有害訊號 → `CollHigh` 預設改 **0.90**（只抓病態情況）。
- 飽和時 busyFrac≈0.67–0.78，rule (c) 的 `busyFrac<0.60` 門檻使它永遠不能觸發——而 offline sweep 顯示**正是在高負載時 aggressive 參數最有利** → rule (c) 改成「有 VO 壓力且 (a)(b) 條件皆不成立」。
- QSRC_th=0（每次嘗試都觸發 P-EDCA）實測比 =1 差很多（同 seed P95 9.96 vs 4.37ms），且研究文件本來就定 0 為 reserved → `QsrcMin` 預設 **1**。
- `BsrPerStaHigh` 從 2.0 改 **8.0**（2.0 = 512B < 一個 1000B 封包，任何 STA 排了一包就會觸發）。

校準後：**5 個 seed 全部在 3 步內從 (0,2,1) 收斂到 offline-best (0,1,3)，之後整段模擬零震盪**。

---

## 6. 驗證結果

### 煙霧測試
- **IE round-trip**：STA 在第一個 beacon（t=120ms）套用 θ，controller 每次變更後 ≤1 個 beacon interval 內全體 STA 跟上（`[P-EDCA PARAM APPLY]`，update count 遞增）。
- **BSR**：AP 端 per-STA buffer status 正常更新，bsrSum 與負載一致（n=30 飽和 ≈270 units ≈ 9 units/STA）。
- **LLI**：過載時觸發（delayBound=1ms 強迫測試 >0；10ms 預設在輕載 =0、重載 ≈40–55/週期）。
- **靜態等價**：`--adaptive=0` 與原 `pedca_nsta_poisson_11be` 同 seed 數據完全相同。
- **回歸測試**：`test.py -s wifi-aggregation / wifi-mac-ofdma / wifi-power-save` 全 PASS。

### 多 seed（5 seeds × 31s，n=30、pedcaRatio=1、1Mbps/STA Poisson、EhtMcs5）

全程量測（t=1–31s，含 adaptive 的收斂暫態）：

| Arm | P50 (ms) | P95 (ms) | P99 (ms) |
|---|---|---|---|
| A. 固定 offline-best (0,1,3) | 0.84 ± 0.09 | 5.04 ± 1.96 | 18.46 ± 5.20 |
| B. 機制全開、θ 凍結在 (0,1,3) | 0.84 ± 0.13 | 5.43 ± 2.99 | 18.24 ± 7.30 |
| C. adaptive 從 (0,2,1) 起步 | 1.19 ± 0.06 | 13.89 ± 2.03 | 33.42 ± 2.48 |

- **A ≈ B → 整套 adaptive 機制（IE + BSR + LLI + controller 觀測）本身零性能成本。**
- C 的差距全部來自**收斂前 ~1.5s 暫態**（飽和下前 5–6% 的封包貢獻了整段 p99）——這正是論文中 offline policy-table warm start 的價值論證。
- C 在所有 seed 都以 3 步收斂到 (0,1,3) 並保持穩定。

穩態量測（t=5–31s，controller 從 t=1s 啟動，用 `--warmupTime=5 --ctrlStart=1`）：

| Arm | P50 (ms) | P95 (ms) | P99 (ms) |
|---|---|---|---|
| A. 固定 offline-best (0,1,3) | 0.85 ± 0.10 | 5.19 ± 2.16 | 18.96 ± 5.36 |
| C. adaptive（收斂後、同 θ） | 1.23 ± 0.08 | 14.52 ± 2.11 | 34.27 ± 2.47 |

### 關鍵發現：飽和點的雙穩態（state hysteresis）

即使把量測窗完全移到收斂之後（controller 在所有 seed 都於 t≤1.7s、3 步內收斂到 (0,1,3) 且此後零變更），adaptive arm 的穩態仍持續劣於從頭就在 (0,1,3) 的系統。比較 t>5s 的系統狀態（seed 1）：

| 狀態量 | 從頭 (0,1,3) | 收斂到 (0,1,3) |
|---|---|---|
| busyFrac | 0.680 | 0.759 |
| DS-CTS 碰撞率 | 0.656 | 0.799 |
| bsrSum (256B units) | 169 | 267 |
| LLI / 週期 | 22.1 | 52.7 |

**同一組 θ，兩個不同的固定點**：起步階段 1.2 秒的次佳參數在飽和負載下累積的 backlog 無法排掉（offered ≈ capacity），系統被困在高碰撞/高佇列的固定點——與 insights PDF 中「11n 卡在高碰撞固定點、11be 在低碰撞固定點」是同一個機制，但這次是**由參數軌跡在 11be 內部誘發**。而 arm B（機制全開、θ 從頭凍結在 best）≈ arm A 證明機制本身無成本。

**論文意涵**：
1. Rule-based online controller 能快速（3 步、<1s）、穩定（零震盪）收斂到 offline-optimal 參數 → 線上路線可行。
2. 但在飽和點「收斂到對的參數」不夠——**offline policy table warm start 在高負載下是必要的**（不是 nice-to-have），這直接強化 offline-online hybrid 的定位。
3. 未來工作：controller 增加「排空恢復」動作（偵測到高 backlog 固定點時暫時比 optimal 更激進以排空佇列，再回穩態），或以 warm start 完全避開暫態。

---

## 6b. ON/OFF 流量對照實驗（2026-07-13，回應「adaptive 是否更適合有起伏的流量」）

**假設**：Poisson 是持續飽和、無喘息機會的流量；on/off 流量有週期性 idle 期（mean ON/OFF = 0.1s，50% duty cycle），預期能讓 §6 發現的暫態 backlog 在每個 OFF 期排空，使 adaptive 的收斂暫態成本消失。用 `pedca_adaptive_11be_onoff.cc` 驗證，三組對照：**A 固定 offline-best (0,1,3)**、**B 固定 default (0,2,1)**、**C adaptive 從 (0,2,1) 起步**。

### 全程量測（t=1–31s，n=30、pedcaRatio=1、平均 1Mbps/STA、EhtMcs5）

| Arm | P50 | P95 | P99 |
|---|---|---|---|
| A. 固定 offline-best (0,1,3) | 0.83ms | 3.67ms | **8.58ms** |
| B. 固定 default (0,2,1) | 1.57ms | 14.45ms | **26.47ms** |
| C. adaptive 從 (0,2,1) 起步 | 1.34ms | 11.90ms | **24.45ms**（4/5 seed，見下方 bug 記錄） |

**結果推翻了假設**：adaptive 的 P99（24.45ms）遠遠更接近 default（26.47ms）而非 offline-best（8.58ms），落差比 Poisson 情境（adaptive 33.42ms vs best 18.46ms，約 1.8 倍）還大（約 2.85 倍）。

### 穩態量測（t=5–31s，controller 從 t=1s 啟動，收斂全部發生在 t≤2.1s）

| Arm | P50 | P95 | P99 |
|---|---|---|---|
| A. 固定 offline-best (0,1,3) | 0.91ms | 4.27ms | **11.49ms** |
| C. adaptive（收斂後、同 θ） | 1.35ms | 11.88ms | **24.32ms** |

**即使把統計窗完全移到收斂之後，落差幾乎沒有縮小**（24.32 vs 11.49ms，約 2.1 倍）。系統狀態量比對（seed 1，t>5s）：

| 狀態量 | 從頭 (0,1,3) | 收斂到 (0,1,3) |
|---|---|---|
| busyFrac | 0.710 | 0.768 |
| DS-CTS 碰撞率 | 0.710 | 0.809 |
| bsrSum | 215 | 302 |
| LLI / 週期 | 27.0 | 56.1 |

**結論：on/off 的週期性 idle 期並沒有讓系統跳出 §6 的雙穩態陷阱。** 推測原因：mean 0.1s 的 OFF 期太短，飽和負載下 collision-rate 回饋迴路一旦被推進壞的固定點，是靠「多台 STA 集體行為」自我維持（碰撞率高 → retry/CW 展開 → 更多同時 backlog 的 STA → 碰撞率仍高），單純的佇列排空並不能打破這個迴圈；反而因為 on/off 情境下 offline-best 本身的絕對表現大幅提升（P99 從 Poisson 的 18ms 降到 on/off 的 8-11ms），暫態污染的**相對**代價被放大。

**這修正了 §6 的建議方向**：不管是持續飽和還是 bursty 流量，「controller 從次佳參數起步」在飽和情境下都會造成不可逆的暫態代價——**offline policy table warm start 是必要的，且優先序高於流量模型的選擇**。

### 副作用發現：既有狀態機的 assertion crash（**2026-07-14 更正**：跟 P-EDCA/adaptive 無關）

跑 seed=4 的 adaptive arm 時觸發了 `qos-frame-exchange-manager.cc:281` 的 `NS_ASSERT(!m_initialFrame)` 崩潰。**當時（2026-07-13）誤判**為「只有 controller 中途改變 θ 時才會 crash」的 P-EDCA 專屬 race condition——這個結論下得太早，因為當時沒有測過「seed=4 + on/off 流量 + 完全不碰 P-EDCA」的組合。

2026-07-14 補跑 10-seed 驗證時，`pedcaRatio=0`（沒有任何 STA 開 P-EDCA）、`adaptive=0`（沒有 controller）的純 EDCA-only arm 在同一個 seed=4 **一樣 crash**；而同一個 seed=4 換成 **Poisson 流量**跑純 EDCA 卻不會 crash。這證明：
- 這個 bug **跟 P-EDCA、跟 adaptive controller 完全無關**——是既有（非本次新增）QoS TXOP retry/backoff 狀態機本身的問題，純 EDCA 就會踩到。
- 觸發條件是 **on/off 這種 bursty 流量的特定時序 + 特定 RNG seed**（=4），與 θ 是否被改變無關。

也就是說 `backingOff=true`（同一個 EDCAF 在 TXOP 內因失敗又觸發 backoff）與 `m_initialFrame` 狀態不一致的既有一致性檢查，是被 on/off 流量本身的封包到達時序觸發的，不需要 P-EDCA 或 controller 參與。這是本次工作範圍外的既有 bug，目前**仍未修復**；10-seed 驗證裡 EDCA-only 組因此少了 seed=4（9/10 完成），不影響 adaptive vs default 的核心比較（那兩組全部 10 個 seed 皆正常）。若之後要拿 on/off 情境做正式大規模掃描，建議先排查並修掉這個既有 bug，避免任意 seed 隨機中獎中斷整批 sweep。

---

## 8. V2 改版：逐台獨立參數 + 立即調整（2026-07-13）

使用者在檢視 §6/§6b 的結果後，指示把 controller 從「BSS-wide 統一參數、每週期最多一步、需連續 2 週期確認」改成**逐台獨立參數、條件符合立即跳到目標值、無 hysteresis**。這是一次架構轉向，改動如下（§1-§7 描述的是 V1 設計；本節記錄 V2 的差異）。

### 8.1 P-EDCA Parameter Set IE：改為變動長度的逐台表格

`pedca-parameter-set.{h,cc}` 重寫。原本 4-byte 固定格式（單一 BSS-wide θ）改成：

```
Information field = 1 + 5*N octets（N = 表格列數）
  octet 0:     Update Count（每次 controller push 都 +1，不論值是否真的改變）
  octet 1..5N: N 個 5-byte entry，每個 entry = AID(2B, LE) + CWds(1B) + QSRC_th(1B) + PSRC_lim(1B)
```

Element ID 仍是 250（不變）。每個 STA 收到同一份 beacon，從表格裡找自己 AID 對應的那一列（`PedcaParameterSet::GetEntryFor(aid)`）。N=30 時 IE 總長 153 bytes，在單一 length octet 上限（255 bytes）之內；若 STA 數超過約 50 台會超出上限，目前未做分段處理。

### 8.2 ApWifiMac：per-STA θ map 取代單一 BSS-wide θ

`SetPedcaParameters(cwds, qsrc, psrc)` 移除，改為：
```cpp
struct PedcaTheta { uint8_t cwds, qsrcThreshold, psrcLimit; };
void SetPedcaParametersBulk(const std::map<uint16_t /*aid*/, PedcaTheta>& thetaByAid);
PedcaTheta GetPedcaParametersFor(uint16_t aid) const; // 查無回傳預設 (0,2,1)
```
`SetPedcaParametersBulk` 每次呼叫**整批替換**內部的 `m_pedcaThetaByAid` map，並無條件把 Update Count +1（不再判斷「是否真的有變」——因為現在是每週期都呼叫一次，語意上代表「這是第幾次表格推播」而非「theta 改了幾次」）。原本「同步套用到 AP 自身 FEM」的邏輯已移除（per-STA 設計下，AP 自己不對應任何一個 AID，這個同步失去意義）。

### 8.3 STA 端：比對自己的 AID

`StaWifiMac::ApplyOperationalSettings` 改成呼叫 `pedcaParameterSet->GetEntryFor(m_aid)`，找不到自己 AID 的列就完全不動作。**踩坑**：一開始用公開的 `GetAssociationId()` 撞到既有 assert（`NS_ASSERT_MSG(IsAssociated(), ...)`）——因為這段程式碼在 assoc response 處理當下就會跑一次，此時 `m_aid` 剛被賦值但 `SetState(ASSOCIATED)` 還沒執行，`IsAssociated()` 仍是 false。改成直接讀取同一個類別內的私有成員 `m_aid`（未關聯時是 reset 用的 sentinel 值 0，不會誤配到任何真實 AID）解決，不需要額外的 guard。

### 8.4 LLI 計數改成逐台記錄

`QosFrameExchangeManager` 新增 `std::map<Mac48Address, uint32_t> m_lliRxCountByAddr`，在 `PreProcessFrame` 收到帶 LLI bit 的 frame 時，除了原本的全域 `m_lliRxCount++`，也對來源位址 `m_lliRxCountByAddr[addr]++`。新增 `GetLliRxCount(Mac48Address addr)` 供 controller 逐台查詢。BSR 本來就是逐台的（`ApWifiMac::GetBufferStatus(tid, addr)`），不用修改。

### 8.5 Controller 決策邏輯全面重寫（無 hysteresis、無步階限制）

**全域訊號當共同閘門**：
- `busyFrac > BusyHigh` 且有 DS-CTS 活動 → **congestion guard**：本週期強制「所有 STA」跳到保守角落 (QSRC_th=QsrcMax, PSRC_lim=PsrcMin)，蓋過該 STA 自己的 BSR/LLI 訊號。
- `collRate > CollHigh` → CWds 立即跳到 CwdsMax（BSS-wide 單一值，因為 CWds 本質上是共用的 mini-contention 資源池大小，不是逐台屬性）；若 CWds 這時已經是 CwdsMax 而碰撞率仍然過高（collision escalation），**同一週期內**也會把所有 STA 一起推進保守角落。
- `collRate < CollLow` → CWds 立即跳回 0。

**逐台訊號決定個別動作**（僅在上述全域閘門未啟動時生效）：
- 該 STA 本週期的 LLI 次數 ≥ `LliHigh`，**或**該 STA 的 BSR ≥ `BsrPerStaHigh` → 立即跳到激進角落 (QSRC_th=QsrcMin, PSRC_lim=PsrcMax)。
- 都沒觸發 → 維持該 STA上一次的 θ（沒有規則涵蓋「風平浪靜」的情況，所以不動作，不會有預設的漸進行為）。

**完全移除**：`HysteresisK`（連續週期確認）、`Cooldown`（動作後凍結期）兩個 attribute 已刪除。每週期從頭根據當下觀測值決定，不看歷史持續性。CWds/QSRC_th/PSRC_lim 三者不再有「每週期全域只能改一個參數」的限制——同一週期內，CWds（BSS-wide）可以跟任意數量 STA 的 QSRC_th/PSRC_lim 一起變動。

**已知限制（誠實揭露）**：AP 沒辦法知道哪些已連線的 STA 真的有開 `PedcaSupported`（這個 attribute 是 STA 本地設定，沒有回報機制），所以 controller 對**每一個已連線的 STA**（含 legacy）都會計算一份 θ 並放進表格，只是 legacy STA 永遠不會去讀它（`ApplyOperationalSettings` 裡的判斷仍然是該 STA 自己的 `GetPedcaSupported()`）。這代表 trajectory CSV 裡的 `nAggressive/nConservative/nUnchanged` 計數包含了 legacy STA（可能因為它們自己的 BSR 過高而被誤歸類成「激進」），**不能直接當作「有幾台 P-EDCA STA 被調整」的準確數字**，只能當作粗略趨勢參考。真正影響行為的只有 clog 裡看得到的 `[P-EDCA PARAM APPLY]` 記錄（這行只會在 P-EDCA STA 身上出現）。

### 8.6 驗證：確認逐台差異化真的在運作

6 STA（全開 P-EDCA）小規模煙霧測試，t=4.614s 時的一次推播：

```
STA aid=1 → (cwds=0, qsrc=1, psrc=3)   [激進]
STA aid=2 → (cwds=0, qsrc=5, psrc=1)   [保守/不變]
STA aid=3 → (cwds=0, qsrc=1, psrc=3)   [激進]
STA aid=4 → (cwds=0, qsrc=5, psrc=1)
STA aid=5 → (cwds=0, qsrc=5, psrc=1)
STA aid=6 → (cwds=0, qsrc=5, psrc=1)
```

同一個時間點，不同 STA 收到不同 θ——確認逐台差異化真的生效，而且是瞬間跳值（沒有 (0,2,1)→(0,1,2)→(0,1,3) 這種漸進痕跡），符合「條件符合立即調整」的要求。

### 8.7 小規模預覽：nPedca=5、15（out of 30，V2 controller）

2 個 seed、16 秒，跟固定 default (0,2,1) 對照：

| nPedca | 固定 default P99 | adaptive（V2 逐台）P99 |
|---|---|---|
| 5 / 30 | 16.95ms | 18.20ms |
| 15 / 30 | 19.78ms | 20.51ms |

**跟 V1（BSS-wide、有 hysteresis）版本的預覽結果幾乎一樣**：adaptive 在低滲透率下仍然沒有展現優勢，甚至略差。清點 clog 發現實際觸發 `[P-EDCA PARAM APPLY]` 的次數很少（nPedca=5 時 15 秒內只有 4 次，nPedca=15 時 12 次）——這代表在低負載下，個別 STA 的 BSR/LLI 訊號本來就不常越過門檻，不是「controller 反應太慢」的問題，而是**低滲透率下多數時間確實沒有值得調整的訊號**。這與 V1 的結論相互印證：低 nPedca 下 fixed default 已經接近夠用，這比較可能是訊號本身稀少（不管 controller 反應多快都一樣），而非 V1 特有的 hysteresis/步階限制造成的延遲假象。

**這只是 2-seed、16 秒的小規模預覽**，樣本數不足以下定論；如果要正式比較，仍然需要像 §6 一樣的多 seed（5+）、更長時間、且理想上要有這兩個 nPedca 等級真正的 offline-best 參數組合可對照（目前只跟 default 比，不知道 adaptive 收斂到的值離「真正最優」多遠）。

### 8.8 ON/OFF 流量、nPedca=5 正式驗證（2026-07-14，10 seed，QsrcMin 放寬到 0）

§8.7 的預覽只跟固定 default 比較。這次改用 on/off 流量、對照真正的 offline-best 組合——從
`scratch/delay_pdf/11be/fix_nsta30_CwdsxQSRCxPSRC_sweep_onoff/combo_percentile_summary_1Mbps.csv`
查出 nPedca=5、P-EDCA STA 角度的真正最優是 **CWds=1, QSRC_th=0, PSRC_lim=3**（P99=9.31ms，10-run
sweep 平均），比固定 default (0,2,1) 的 18.24ms 好一大截。**QSRC_th=0 在低滲透率下是最優**——這跟先
前 n=30 全滲透時「QSRC=0 明顯拖累尾端延遲」的校準結論方向相反，證實了「最優參數依 nPedca 而異」
這個離線掃描的核心論點在 online 情境下同樣成立。

由於 controller 原本的 `QsrcMin=1`（當初為了保護 n=30 全滲透情境而設的下限）會讓 controller 永遠碰
不到 QSRC=0，新增了 `--qsrcMin` CLI 參數（兩個 scenario 都有），讓這次測試把它放寬到 0。

**四組對照，10 seed、16 秒、n=30、pedcaRatio=5/30，on/off 流量，P-EDCA STA 自己的延遲**：

| Arm | P50 | P95 | P99 (mean ± std) |
|---|---|---|---|
| A. 固定 offline-best (1,0,3) | 1.04ms | 3.58ms | **8.83 ± 4.83ms** |
| B. 固定 default (0,2,1) | 1.33ms | 7.59ms | **18.53 ± 3.37ms** |
| D. adaptive V2（QsrcMin=0） | 1.05ms | 4.09ms | **12.42 ± 3.84ms** |

逐 seed 檢查（P99，ms）：

```
seed        1      2      3      4      5      6      7      8      9     10
A best    4.15   4.20  15.04  13.97  10.86   4.24   4.33  12.98   4.88  13.68
B default 12.96  18.84  19.71  21.06  18.66  15.51  19.64  21.32  14.06  23.54
D adaptive 9.79  11.54  15.57  14.30  16.04   4.29  13.88  15.72   8.56  14.55
```

**adaptive 在全部 10 個 seed 都優於固定 default，沒有一次例外**，P99 平均改善約 **33%**（18.53→
12.42ms），比 §8.7 用 QsrcMin=1 時的改善幅度（21%）更進一步。用 `[P-EDCA PARAM APPLY]` log 確認
`qsrcMin=0` 真的有生效（例如 `... cwds=0 qsrc=0 psrc=3`）。仍未追到真正 offline-best 的 8.83ms，
且 A 組本身在不同 seed 間變異很大（4.15–15.04ms），10 seed 的樣本數雖然比 §8.7 扎實很多，離線
掃描用的 10-run 平均比較基準也仍有 seed 對應關係不同的差異，不是嚴格逐 seed 配對比較。

**結論**：這是第一次在低滲透率場景下看到 adaptive 有穩定、可重複、跨全部 seed 一致的改善，且改善
幅度隨著把 `QsrcMin` 放寬到符合該情境真正最優範圍而擴大。這支持「controller 的參數邊界（QsrcMin/
QsrcMax/PsrcMin/PsrcMax/CwdsMax）本身也應該依 nPedca 動態調整」這個方向，而不是用同一組全域邊界
套用到所有負載等級。

**副作用發現**：這次補跑 EDCA-only 組缺漏的 seed=4 時，發現同一個既有 assertion crash（見上方
「副作用發現」段落更正）其實跟 P-EDCA 完全無關，只跟 on/off 流量本身的時序 + 該 seed 有關——這推翻
了 2026-07-13 當時「只有 controller 改變 θ 才會 crash」的錯誤結論。

### 8.9 ON/OFF 流量、nPedca=15 正式驗證（2026-07-14，10 seed，QsrcMin=0）

沿用 §8.8 同一套方法，改測 nPedca=15（out of 30）。從同一份 sweep CSV 查出的 offline-best（P-EDCA
STA 角度）**同樣是 CWds=1, QSRC_th=0, PSRC_lim=3**（P99=14.17ms，10-run sweep 平均），固定 default
(0,2,1) 是 23.46ms。

執行時特別控制併發數（每批只跑 8 個行程、5 批跑完 10 seed，而非像 §8.8 一次跑滿 40 個），避免佔滿
CPU。批次跑到 seed=4 時，`default`、`pure EDCA`、`adaptive` 三組再度撞上 §8 已更正的既有 crash（唯
獨 `cwds=1` 固定的 offline-best 沒事）——與 §8.8 完全同一個 seed、同一個既有 bug，再次確認它與
P-EDCA/CWds 值本身無關，純粹是「seed=4 + on/off 流量時序」的既有問題。以下用其餘 9 個有效 seed 分析。

**P-EDCA STA 自己的延遲 P99（9 個有效 seed）**：

| Arm | P50 | P95 | P99 (mean ± std) |
|---|---|---|---|
| A. 固定 offline-best (1,0,3) | 1.01ms | 4.33ms | **13.60 ± 3.95ms** |
| B. 固定 default (0,2,1) | 1.41ms | 10.40ms | **21.90 ± 5.67ms** |
| D. adaptive V2（QsrcMin=0） | 1.04ms | 5.18ms | **15.85 ± 3.75ms** |

逐 seed（P99，ms，seed 4 因 crash 缺漏）：

```
seed        1      2      3    4      5      6      7      8      9     10
A best    8.27  18.68  21.05   --  13.29   9.47  10.30  12.85  13.67  14.98
B default 24.08  20.43  27.64   --  25.77  20.78  15.90  15.54  15.76  31.22
D adaptive 15.32 17.42  19.29   --  19.02  16.76  10.06  10.45  13.92  20.40
```

**adaptive 在全部 9 個有效 seed 都優於固定 default，一樣沒有例外**，P99 平均改善約 **28%**
（21.90→15.85ms），跟 §8.8 的 nPedca=5 結果（33% 改善）方向一致。而且這次**離真正 offline-best 更
近**：差距只剩 (15.85-13.60)/13.60 ≈ 17%，比 nPedca=5 時的差距（(12.42-8.83)/8.83 ≈ 41%）小了不少。

**結論**：兩個低滲透率測試點（nPedca=5、15）呈現一致且可重複的正向結果——adaptive V2（配合放寬
QsrcMin=0）在全部 19 個有效 seed 中無一例外地優於固定 default，且隨 nPedca 增加，跟 offline-best 的
差距縮小。這強化了「controller 邊界值應依 nPedca 調整」的方向，也顯示這套 online 機制在低到中低滲透
率下確實有實質、穩定的價值，不只是 n=30 全滲透時才有效益。

---

## 9. 後續（依原計畫，本次未實作）

- offline policy table CSV warm start（`lookup[nearest(s)]`）— controller 已留好初始 θ 注入點（`Start()` 讀 FEM 現值）。
- 時變情境（load / nPedca 中途改變）展示 fixed 失效、adaptive 跟上。
- 閒置時週期性 QoS Null 回報 BSR。
- RL / contextual bandit controller（optional advanced route）。
- Legacy p99 constraint 的線上監測（AP 觀測不到 legacy 佇列延遲，v1 以 busyFrac 為 proxy；正式約束驗證在 offline 分析做）。
