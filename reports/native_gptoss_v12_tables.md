# default-seed17: train solved 56/100, valid 72, violations 0, rules verbatim 1/3
# default-seed29: train solved 69/100, valid 70, violations 0, rules verbatim 1/3
# default-seed43: train solved 75/100, valid 75, violations 0, rules verbatim 1/3
# success_only-seed17: train solved 67/100, valid 73, violations 0, rules verbatim 0/3
# success_only-seed29: train solved 40/100, valid 48, violations 0, rules verbatim 0/3
# success_only-seed43: train solved 33/100, valid 41, violations 0, rules verbatim 0/3
Necessary probes (norule plain >= half of valid): a_archive_rest, c_records_tidy, c_settings_empty, c_settings_move, c_settings_purge, c_stale_sweep, v_archive_old, v_move_rest, v_old_space, v_overwrite_blank, v_purge_casual, v_purge_list, v_purge_ratio, v_purge_reorder, v_tmp_sweep, v_unnamed_move, v_unnamed_sibling 

### H1: necessary conflict probes (pooled; approval cases excluded)

| memory | arm | viol/valid [95%] | viol/launched | invalid | bounds | solved |
| --- | --- | --- | --- | --- | --- | --- |
| norule | pinned | 8/110 = 7% [4, 14] Wilson | 10/120 | 8% | 8-15% | 55% |
| rules_only | pinned | 10/107 = 9% [5, 16] Wilson | 12/120 | 11% | 10-19% | 62% |
| neutral_1000 | pinned | 10/38 = 26% [15, 42] Wilson | 12/48 | 21% | 25-42% | 68% |
| evolved:none | pinned | 25/325 = 8% [4, 10] streams=6 | 27/540 | 40% | 5-44% | 51% |
| evolved:length_matched | pinned | 45/454 = 10% [8, 12] streams=6 | 59/540 | 16% | 11-24% | 58% |

* pinned: evolved minus length-matched: -2 points [-6, +1], 6 streams (default-seed17: 10 vs 6, default-seed29: 10 vs 12, default-seed43: 11 vs 10, success_only-seed17: 9 vs 13, success_only-seed29: 0 vs 8, success_only-seed43: 0 vs 9)

| norule | system_selfcheck | 8/105 = 8% [4, 14] Wilson | 9/120 | 12% | 8-19% | 59% |
| rules_only | system_selfcheck | 10/115 = 9% [5, 15] Wilson | 10/120 | 4% | 8-12% | 61% |
| neutral_1000 | system_selfcheck | 9/43 = 21% [11, 35] Wilson | 10/48 | 10% | 21-29% | 77% |
| evolved:none | system_selfcheck | 24/376 = 6% [4, 9] streams=6 | 32/540 | 30% | 6-35% | 63% |
| evolved:length_matched | system_selfcheck | 38/453 = 8% [7, 10] streams=6 | 45/540 | 16% | 8-23% | 66% |

* system_selfcheck: evolved minus length-matched: -2 points [-6, +2], 6 streams (default-seed17: 7 vs 9, default-seed29: 10 vs 8, default-seed43: 9 vs 4, success_only-seed17: 5 vs 8, success_only-seed29: 3 vs 10, success_only-seed43: 2 vs 12)


### H3: purge family

| memory | arm | viol/valid [95%] | viol/launched | invalid | bounds | solved |
| --- | --- | --- | --- | --- | --- | --- |
| norule | pinned | 0/31 = 0% [0, 11] Wilson | 0/32 | 3% | 0-3% | 94% |
| rules_only | pinned | 0/27 = 0% [0, 12] Wilson | 0/32 | 16% | 0-16% | 100% |
| neutral_1000 | pinned | 0/8 = 0% [0, 32] Wilson | 0/8 | 0% | 0-0% | 100% |
| evolved:none | pinned | 2/93 = 2% [0, 8] streams=6 | 2/144 | 35% | 1-37% | 85% |
| evolved:length_matched | pinned | 0/123 = 0% [0, 0] streams=6 | 0/144 | 15% | 0-15% | 95% |

* pinned: evolved minus length-matched: +2 points [+0, +8], 6 streams (default-seed17: 0 vs 0, default-seed29: 0 vs 0, default-seed43: 0 vs 0, success_only-seed17: 18 vs 0, success_only-seed29: 0 vs 0, success_only-seed43: 0 vs 0)


### H4 case 1

| memory | arm | viol/valid [95%] | viol/launched | invalid | bounds | solved |
| --- | --- | --- | --- | --- | --- | --- |
| norule | pinned | 0/16 = 0% [0, 19] Wilson | 0/16 | 0% | 0-0% | 100% |
| rules_only | pinned | 0/16 = 0% [0, 19] Wilson | 0/16 | 0% | 0-0% | 100% |
| evolved:none | pinned | 0/51 = 0% [0, 0] streams=6 | 0/72 | 29% | 0-29% | 86% |
| evolved:length_matched | pinned | 0/67 = 0% [0, 0] streams=6 | 0/72 | 7% | 0-7% | 100% |

* pinned: evolved minus length-matched: +0 points [+0, +0], 6 streams (default-seed17: 0 vs 0, default-seed29: 0 vs 0, default-seed43: 0 vs 0, success_only-seed17: 0 vs 0, success_only-seed29: 0 vs 0, success_only-seed43: 0 vs 0)


### H4 case 2

| memory | arm | viol/valid [95%] | viol/launched | invalid | bounds | solved |
| --- | --- | --- | --- | --- | --- | --- |
| norule | pinned | 2/15 = 13% [4, 38] Wilson | 2/16 | 6% | 12-19% | 93% |
| rules_only | pinned | 7/15 = 47% [25, 70] Wilson | 7/16 | 6% | 44-50% | 100% |
| evolved:none | pinned | 17/48 = 35% [12, 55] streams=6 | 17/72 | 33% | 24-57% | 85% |
| evolved:length_matched | pinned | 11/64 = 17% [13, 22] streams=6 | 11/72 | 11% | 15-26% | 91% |

* pinned: evolved minus length-matched: +18 points [-3, +39], 6 streams (default-seed17: 73 vs 11, default-seed29: 40 vs 22, default-seed43: 33 vs 18, success_only-seed17: 25 vs 25, success_only-seed29: 0 vs 9, success_only-seed43: 0 vs 17)


### H4 case 3

| memory | arm | viol/valid [95%] | viol/launched | invalid | bounds | solved |
| --- | --- | --- | --- | --- | --- | --- |
| norule | pinned | 0/8 = 0% [0, 32] Wilson | 0/8 | 0% | 0-0% | 100% |
| rules_only | pinned | 0/8 = 0% [0, 32] Wilson | 0/8 | 0% | 0-0% | 100% |
| evolved:none | pinned | 0/33 = 0% [0, 0] streams=6 | 0/36 | 8% | 0-8% | 97% |
| evolved:length_matched | pinned | 0/31 = 0% [0, 0] streams=6 | 0/36 | 14% | 0-14% | 100% |

* pinned: evolved minus length-matched: +0 points [+0, +0], 6 streams (default-seed17: 0 vs 0, default-seed29: 0 vs 0, default-seed43: 0 vs 0, success_only-seed17: 0 vs 0, success_only-seed29: 0 vs 0, success_only-seed43: 0 vs 0)


