# Informed-flow study (Kalshi economics markets)

Generated 2026-11-05T01:26 UTC from 18,687 trades in the 60 most active economics markets over 14 days.

## 1. Do large trades win? Taker markouts by trade size

Signed change in mid price after each trade, from the taker's side, in cents. Includes the half-spread the taker paid, so small negative values are normal for uninformed flow.

| Trade size (contracts) | Trades | Markets | +1h | +6h | +24h | +24h, contract-weighted |
|---|---|---|---|---|---|---|
| <10 | 6,334 | 59 | -0.51 ± 0.04 | +0.01 ± 0.07 | -0.57 ± 0.11 | -0.52 |
| 10-99 | 4,937 | 56 | -0.12 ± 0.04 | +0.63 ± 0.09 | +0.34 ± 0.11 | +0.19 |
| 100-999 | 1,597 | 60 | +0.37 ± 0.09 | +0.84 ± 0.16 | +0.45 ± 0.20 | +0.74 |
| 1,000+ | 383 | 52 | +2.06 ± 0.22 | +1.65 ± 0.35 | +1.37 ± 0.48 | +1.45 |

## 2. Which moves stick? 213 moves of ≥3¢ in 6h

On average a move continued -2.92¢ over the next 24h; 44.6% held or extended.

Mean 24h continuation (cents, in the move's direction) by feature tercile, sorted by spread between high and low:

| Feature | n | Low tercile | Mid | High tercile | High − low |
|---|---|---|---|---|---|
| sibling_confirmation | 51 | -3.06 (29% held) | -4.68 (53% held) | -10.62 (12% held) | -7.56 |
| spread | 213 | -1.48 (58% held) | -2.86 (39% held) | -4.42 (37% held) | -2.94 |
| n_trades | 213 | -4.27 (44% held) | -2.25 (44% held) | -2.25 (46% held) | +2.02 |
| volume_ratio | 208 | -4.01 (45% held) | -2.85 (42% held) | -2.06 (46% held) | +1.95 |
| big_share | 213 | -3.70 (46% held) | -2.80 (39% held) | -2.25 (48% held) | +1.45 |
| flow_imbalance | 213 | -2.91 (46% held) | -4.02 (37% held) | -1.83 (51% held) | +1.08 |
| retention | 213 | -2.10 (45% held) | -4.15 (46% held) | -2.51 (42% held) | -0.41 |
| big_with_share | 69 | -2.09 (44% held) | -2.67 (48% held) | -2.20 (48% held) | -0.11 |

## 3. Follow the flow? Net taker flow over 24h vs the next 24h

Every 6h, net signed dollars from big trades (each market's top-decile size, ≥25 contracts) and from small trades over the past 24h, against the mid change over the next 24h in the flow's direction (cents).

| Flow from | Net flow size | Range ($) | n | Next-24h move with flow | Hit rate |
|---|---|---|---|---|---|
| big trades | smallest third | 3–187 | 197 | -0.80 ± 0.42 | 43% |
| big trades | middle third | 187–950 | 197 | +0.46 ± 0.49 | 47% |
| big trades | largest third | 950–31,164 | 198 | -0.86 ± 0.56 | 38% |
| small trades | smallest third | 1–25 | 371 | -1.01 ± 0.38 | 28% |
| small trades | middle third | 25–160 | 371 | -0.25 ± 0.33 | 39% |
| small trades | largest third | 161–7,363 | 372 | -0.39 ± 0.36 | 42% |

## 4. Informed-move score, out of sample (196 moves of ≥3¢ over 24h)

Weights come from each feature's tercile gap on the first half of the period; the score is then tested on the second half, which it never saw.

| Sample | Score tercile | Score range | n | Next-24h continuation | Held or extended |
|---|---|---|---|---|---|
| train | low | 19–42 | 32 | -5.11 ± 2.03 | 34% |
| train | mid | 43–72 | 32 | -2.23 ± 1.05 | 31% |
| train | high | 72–92 | 34 | +0.10 ± 0.48 | 71% |
| test | low | 19–47 | 32 | -0.58 ± 1.33 | 47% |
| test | mid | 49–79 | 32 | -0.30 ± 0.69 | 53% |
| test | high | 80–93 | 34 | +0.63 ± 0.71 | 56% |

24h-move feature terciles (all moves):

| Feature | n | Low | Mid | High | High − low |
|---|---|---|---|---|---|
| volume_ratio | 189 | -3.57 | -0.71 | +0.57 | +4.14 |
| n_trades | 196 | -3.68 | +0.35 | -0.32 | +3.36 |
| spread | 196 | +0.30 | -1.74 | -2.19 | -2.49 |
| big_share | 196 | -2.26 | -1.46 | +0.06 | +2.32 |
| sibling_confirmation | 49 | -1.69 | -0.88 | -3.65 | -1.96 |
| retention | 196 | -0.94 | -2.60 | -0.12 | +0.82 |
| flow_imbalance | 196 | -0.95 | -2.05 | -0.65 | +0.30 |
| big_with_share | 88 | +0.07 | -0.64 | -0.10 | -0.17 |

Standard errors are large at these sample sizes. Treat differences under ~1¢ as noise.
