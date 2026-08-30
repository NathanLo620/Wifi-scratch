# Burst-adaptive P-EDCA — On/Off saturation validation

Three paired RngRun seeds, nSta=30, mean offered load 1 Mbps/STA, dual DS-CTS, 1 s warmup and 7 s measurement window. Offline default is (0,2,1); offline best is the existing 10-run sweep's loss-aware R(10 ms) winner (0,4,3).

| nPEDCA | arm | P50 ms | P95 ms | P99 ms | MAC loss % | R(10 ms) % | P99 vs default | R10 vs default |
|---:|:---|---:|---:|---:|---:|---:|---:|---:|
| 5 | EDCA -- | 1.423 | 7.857 | 12.488 | 6.97 | 90.53 | +23.9% | -4.65 pt |
| 5 | offline default (0,2,1) | 1.383 | 4.287 | 10.078 | 3.84 | 95.17 | +0.0% | +0.00 pt |
| 5 | offline best (0,4,3) | 1.363 | 4.603 | 7.567 | 0.34 | 99.33 | -24.9% | +4.16 pt |
| 5 | burst-adaptive dynamic | 1.238 | 5.412 | 12.732 | 0.45 | 97.82 | +26.3% | +2.64 pt |
| 15 | EDCA -- | 1.423 | 7.857 | 12.488 | 6.97 | 90.53 | -2.2% | -1.46 pt |
| 15 | offline default (0,2,1) | 1.512 | 6.367 | 12.768 | 5.92 | 91.99 | +0.0% | +0.00 pt |
| 15 | offline best (0,4,3) | 1.587 | 6.282 | 10.158 | 1.47 | 97.45 | -20.4% | +5.47 pt |
| 15 | burst-adaptive dynamic | 1.468 | 7.893 | 16.122 | 1.16 | 95.73 | +26.3% | +3.75 pt |
| 30 | EDCA -- | 1.423 | 7.857 | 12.488 | 6.97 | 90.53 | -11.1% | +1.64 pt |
| 30 | offline default (0,2,1) | 1.502 | 8.182 | 14.047 | 8.06 | 88.89 | +0.0% | +0.00 pt |
| 30 | offline best (0,4,3) | 1.653 | 7.423 | 12.182 | 3.22 | 94.82 | -13.3% | +5.94 pt |
| 30 | burst-adaptive dynamic | 1.478 | 8.898 | 17.427 | 2.20 | 93.93 | +24.1% | +5.04 pt |

## Adaptive trajectory

| nPEDCA | modal QSRC | q2 | q3 | q4 | q5 | mean max k-hat | estimated Stage-1 airtime |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 5 | 3 | 6.3% | 93.7% | 0.0% | 0.0% | 4.7 | 2.21% |
| 15 | 4 | 1.4% | 13.5% | 85.0% | 0.0% | 12.0 | 4.04% |
| 30 | 4 | 0.5% | 1.4% | 98.1% | 0.0% | 25.7 | 7.14% |

> Caveat: WifiTxStatsHelper loss counts completed MAC drops, while packets still queued at the simulation stop are not counted as losses. R(10 ms) therefore fixes delivered-only percentile bias but is not a full application-level deadline-delivery ratio.
