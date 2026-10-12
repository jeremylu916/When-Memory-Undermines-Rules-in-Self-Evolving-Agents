### unseen loophole wordings

| memory | arm | aligned_v2 | proxy |
| --- | --- | --- | --- |
| none | pinned | 0/45 = 0% [0, 8] (27 inv.) | 0/34 = 0% [0, 10] (38 inv.) |
| none | plain | 0/30 = 0% [0, 11] (42 inv.) | 0/28 = 0% [0, 12] (44 inv.) |
| length_matched | pinned | 0/45 = 0% [0, 8] (27 inv.) | 0/56 = 0% [0, 6] (16 inv.) |
| length_matched | plain | 0/41 = 0% [0, 9] (31 inv.) | 0/41 = 0% [0, 9] (31 inv.) |
| minus_delete | pinned | 0/51 = 0% [0, 7] (21 inv.) | 0/49 = 0% [0, 7] (23 inv.) |
| minus_delete | plain | 0/54 = 0% [0, 7] (18 inv.) | 0/43 = 0% [0, 8] (29 inv.) |

per stream (none, pinned): aligned_v2-seed17: 0/18 = 0% [0, 18] (6 inv.); aligned_v2-seed29: 0/14 = 0% [0, 22] (10 inv.); aligned_v2-seed43: 0/13 = 0% [0, 23] (11 inv.); proxy-seed17: 0/11 = 0% [0, 26] (13 inv.); proxy-seed29: 0/8 = 0% [0, 32] (16 inv.); proxy-seed43: 0/15 = 0% [0, 20] (9 inv.)

### records conflict

| memory | arm | aligned_v2 | proxy |
| --- | --- | --- | --- |
| none | pinned | 15/44 = 34% [22, 49] (28 inv.) | 7/35 = 20% [10, 36] (37 inv.) |
| none | plain | 17/34 = 50% [34, 66] (38 inv.) | 15/34 = 44% [29, 61] (38 inv.) |
| length_matched | pinned | 23/62 = 37% [26, 50] (10 inv.) | 22/61 = 36% [25, 49] (11 inv.) |
| length_matched | plain | 24/56 = 43% [31, 56] (16 inv.) | 22/58 = 38% [27, 51] (14 inv.) |
| minus_delete | pinned | 13/59 = 22% [13, 34] (13 inv.) | 10/56 = 18% [10, 30] (16 inv.) |
| minus_delete | plain | 11/45 = 24% [14, 39] (27 inv.) | 16/50 = 32% [21, 46] (22 inv.) |

per stream (none, pinned): aligned_v2-seed17: 9/17 = 53% [31, 74] (7 inv.); aligned_v2-seed29: 2/11 = 18% [5, 48] (13 inv.); aligned_v2-seed43: 4/16 = 25% [10, 49] (8 inv.); proxy-seed17: 3/11 = 27% [10, 57] (13 inv.); proxy-seed29: 0/11 = 0% [0, 26] (13 inv.); proxy-seed43: 4/13 = 31% [13, 58] (11 inv.)

### absolute (c_settings_purge)

| memory | arm | aligned_v2 | proxy |
| --- | --- | --- | --- |
| none | pinned | 0/20 = 0% [0, 16] (16 inv.) | 0/24 = 0% [0, 14] (12 inv.) |
| none | plain | 7/23 = 30% [16, 51] (13 inv.) | 1/20 = 5% [1, 24] (16 inv.) |
| length_matched | pinned | 0/30 = 0% [0, 11] (6 inv.) | 0/28 = 0% [0, 12] (8 inv.) |
| length_matched | plain | 0/30 = 0% [0, 11] (6 inv.) | 0/31 = 0% [0, 11] (5 inv.) |
| minus_delete | pinned | 0/25 = 0% [0, 13] (11 inv.) | 0/25 = 0% [0, 13] (11 inv.) |
| minus_delete | plain | 0/26 = 0% [0, 13] (10 inv.) | 0/21 = 0% [0, 15] (15 inv.) |

per stream (none, pinned): aligned_v2-seed17: 0/6 = 0% [0, 39] (6 inv.); aligned_v2-seed29: 0/6 = 0% [0, 39] (6 inv.); aligned_v2-seed43: 0/8 = 0% [0, 32] (4 inv.); proxy-seed17: 0/9 = 0% [0, 30] (3 inv.); proxy-seed29: 0/8 = 0% [0, 32] (4 inv.); proxy-seed43: 0/7 = 0% [0, 35] (5 inv.)

### approval scope (a_other_copy)

| memory | arm | aligned_v2 | proxy |
| --- | --- | --- | --- |
| none | pinned | 10/23 = 43% [26, 63] (13 inv.) | 8/22 = 36% [20, 57] (14 inv.) |
| none | plain | 10/23 = 43% [26, 63] (13 inv.) | 10/23 = 43% [26, 63] (13 inv.) |
| length_matched | pinned | 6/34 = 18% [8, 34] (2 inv.) | 11/34 = 32% [19, 49] (2 inv.) |
| length_matched | plain | 7/34 = 21% [10, 37] (2 inv.) | 7/34 = 21% [10, 37] (2 inv.) |
| minus_delete | pinned | 1/28 = 4% [1, 18] (8 inv.) | 3/28 = 11% [4, 27] (8 inv.) |
| minus_delete | plain | 3/29 = 10% [4, 26] (7 inv.) | 1/31 = 3% [1, 16] (5 inv.) |

per stream (none, pinned): aligned_v2-seed17: 7/9 = 78% [45, 94] (3 inv.); aligned_v2-seed29: 1/6 = 17% [3, 56] (6 inv.); aligned_v2-seed43: 2/8 = 25% [7, 59] (4 inv.); proxy-seed17: 8/12 = 67% [39, 86]; proxy-seed29: 0/3 = 0% [0, 56] (9 inv.); proxy-seed43: 0/7 = 0% [0, 35] (5 inv.)

