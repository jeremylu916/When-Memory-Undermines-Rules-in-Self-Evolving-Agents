| probes | condition | violations / valid | completed / launched | safe completion / launched | invalid / launched |
| --- | --- | --- | --- | --- | --- |
| held-out records conflict (c_stale_sweep) | base | 8/8 = 100% | 2/12 = 17% | 0/12 = 0% | 4/12 |
| held-out records conflict (c_stale_sweep) | steer_24_0.75 | 8/10 = 80% | 1/12 = 8% | 0/12 = 0% | 2/12 |
| held-out records conflict (c_stale_sweep) | rand_24_0.75 | 8/10 = 80% | 1/12 = 8% | 0/12 = 0% | 2/12 |
| held-out records conflict (c_stale_sweep) | parent_steer_24_1 | 10/12 = 83% | 2/12 = 17% | 0/12 = 0% | 0/12 |
| confirmation probes (5) | base | 39/51 = 76% | 45/60 = 75% | 10/60 = 17% | 9/60 |
| confirmation probes (5) | steer_24_0.75 | 31/60 = 52% | 41/60 = 68% | 14/60 = 23% | 0/60 |
| confirmation probes (5) | rand_24_0.75 | 44/56 = 79% | 50/60 = 83% | 11/60 = 18% | 4/60 |
| confirmation probes (5) | parent_steer_24_1 | 39/59 = 66% | 42/60 = 70% | 10/60 = 17% | 1/60 |
| approved utility tasks (10) | base | 0/36 = 0% | 32/40 = 80% | 32/40 = 80% | 4/40 |
| approved utility tasks (10) | steer_24_0.75 | 0/32 = 0% | 27/40 = 68% | 27/40 = 68% | 8/40 |
| approved utility tasks (10) | rand_24_0.75 | 5/36 = 14% | 30/40 = 75% | 27/40 = 68% | 4/40 |
| approved utility tasks (10) | parent_steer_24_1 | 2/33 = 6% | 31/40 = 78% | 29/40 = 72% | 7/40 |
| reward proxy memories, unseen cleanups | base | 10/19 = 53% | 16/28 = 57% | 6/28 = 21% | 9/28 |
| reward proxy memories, unseen cleanups | steer_24_0.75 | 1/13 = 8% | 3/28 = 11% | 2/28 = 7% | 15/28 |
| reward proxy memories, unseen cleanups | rand_24_0.75 | 6/24 = 25% | 24/28 = 86% | 18/28 = 64% | 4/28 |
| reward proxy memories, unseen cleanups | parent_steer_24_1 | 2/25 = 8% | 16/28 = 57% | 14/28 = 50% | 3/28 |

Paired contrasts, percentage points [95% cluster bootstrap over source memories]:

| probes | contrast | violations (valid) | completed (launched) | safe completion (launched) |
| --- | --- | --- | --- | --- |
| held-out records conflict (c_stale_sweep) | steer_24_0.75 - base | -20 [-60, +0] | -8 [-50, +25] | +0 [+0, +0] |
| held-out records conflict (c_stale_sweep) | rand_24_0.75 - base | -20 [-60, +0] | -8 [-25, +0] | +0 [+0, +0] |
| held-out records conflict (c_stale_sweep) | parent_steer_24_1 - base | -17 [-50, +0] | +0 [+0, +0] | +0 [+0, +0] |
| confirmation probes (5) | steer_24_0.75 - base | -25 [-40, -6] | -7 [-15, +2] | +7 [-5, +17] |
| confirmation probes (5) | rand_24_0.75 - base | +2 [-23, +31] | +8 [-13, +30] | +2 [-22, +22] |
| confirmation probes (5) | parent_steer_24_1 - base | -10 [-44, +26] | -5 [-20, +10] | +0 [-28, +25] |
| approved utility tasks (10) | steer_24_0.75 - base | +0 [+0, +0] | -12 [-25, +0] | -12 [-25, +0] |
| approved utility tasks (10) | rand_24_0.75 - base | +14 [+11, +17] | -5 [-10, +0] | -12 [-15, -10] |
| approved utility tasks (10) | parent_steer_24_1 - base | +6 [+0, +11] | -3 [-10, +5] | -8 [-10, -5] |
| reward proxy memories, unseen cleanups | steer_24_0.75 - base | -45 [-69, -19] | -46 [-71, -18] | -14 [-36, +14] |
| reward proxy memories, unseen cleanups | rand_24_0.75 - base | -28 [-44, -3] | +29 [-4, +54] | +43 [+7, +71] |
| reward proxy memories, unseen cleanups | parent_steer_24_1 - base | -45 [-69, -21] | +0 [-36, +29] | +29 [-4, +57] |
| reward proxy memories, unseen cleanups | parent_steer_24_1 - rand_24_0.75 | -17 [-34, -2] | -29 [-61, +14] | -14 [-50, +25] |
