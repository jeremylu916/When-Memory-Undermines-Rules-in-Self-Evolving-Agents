# DeepSeek Flash computer-use safety pilot

The user authorized DeepSeek V4.1 Flash experiments within a ¥100 account
balance. The local virtual-desktop experiments use the documented
`deepseek-flash` API alias, non-thinking mode, JSON output, temperature 0,
and `max_tokens=1024`. The alias is mutable; the ledgers retain the returned
model and system fingerprint. DeepSeek does not document a chat-completions
`seed` field, so local stream/trial seeds were not sent to the provider.

This pilot uses three generated held-out workflows, two trials each, six
training episodes, and checkpoints at 0 and 6. The initial strict-parser
run is under `runs/deepseek_flash_pilot_20260930/`. It exposed an interface
issue: the model often returned a valid bare GUI action rather than the
requested `{"action": ...}` envelope. Those outputs were marked invalid by
the initial parser. The follow-up run under
`runs/deepseek_flash_pilot_schemafix_20260930/` accepts exactly either
form, while the desktop still rejects malformed actions and invisible
targets. It is a post-fix feasibility run, not a preregistered replication.

| Condition | Valid current probes at 0 / 6 | Task success at 0 / 6 | Attempted safety violations at 0 / 6 | Accepted memory updates |
| --- | ---: | ---: | ---: | ---: |
| Frozen | 6/6 / 6/6 | 100% / 100% | 0 / 0 | 0 |
| Compression only | 6/6 / 6/6 | 100% / 100% | 0 / 0 | 6 |
| Evolving | 6/6 / 6/6 | 100% / 83.3% | 0 / 0 | 6 |

The corrected run made 683 paid requests. It recorded 483,618 prompt tokens,
15,468 completion tokens, and 327,307 prompt-cache-hit tokens. Pricing all
prompt tokens at the peak cache-miss rate gives a conservative local estimate
of $0.163647. Applying the published peak cache-hit/miss rates to the
reported counters gives approximately $0.067419; actual account charges
should be checked from the CNY balance API. All three streams remained
baseline-compliant and had no observed violation through checkpoint 6. The
evolving final memory still contained all three original rule strings.

The [combined comparison](../runs/deepseek_flash_pilot_schemafix_20260930/comparison.json)
verifies source report hashes and yields a zero descriptive difference in
attempted-violation change for evolving versus either control. There is only
one independent memory stream per condition, so no uncertainty interval or
general claim follows. Repeated probe trials are correlated. The tasks use
closely related templates on an accessibility-tree desktop, not a visual or
real host desktop. Some non-current counterfactual replays also produced
invalid GUI actions; they remain excluded from paired effects. The parser
fix was chosen after observing the first run, and model sampling was not
seeded by the provider.

The first long follow-up froze a 100-episode, two-stream manifest with
checkpoints at 0, 25, 50, 75, and 100. Frozen, compression-only, and evolving
conditions ran in six isolated processes, each capped at 2,600 requests and
$1 in conservative local USD accounting. Five streams completed; evolving
seed 17 stopped after its 62nd training episode because a memory rewrite
filled the 1,024-token response limit and yielded truncated JSON. The
[partial comparison](../runs/deepseek_flash_long_horizon_20260930/partial_comparison.json)
contains two frozen, two compression-only, and one evolving stream. All 30
final current-memory probes in those five streams were valid, successful,
and free of forbidden attempts. The lone completed evolving stream retained
all three original rule strings after 99 accepted updates. This is partial
evidence only: the missing evolving stream cannot be counted as safe or
imputed from the other seed.

A second long run raised the memory-rewrite output allowance to 2,048 tokens
while leaving GUI actions at 1,024. One evolving stream then returned a
complete but overlong 4,431-character memory at episode 16. It stopped on
the unchanged 4,000-character budget, as designed. The failure is retained
under `runs/deepseek_flash_long_horizon_memorycap_20260930/`.

The final matched exploratory run at
`runs/deepseek_flash_long_horizon_repair_20260930/` permits up to two
explicit, paid compression attempts when a memory proposal exceeds 4,000
characters. A live preflight reduced that observed 4,431-character proposal
to 2,685 characters while preserving all three rule strings. All six
condition/seed streams were restarted under one new manifest and model
configuration. The procedure and call caps are recorded in its campaign
manifest. These post-hoc interface fixes make the run usable as feasibility
evidence; they do not turn it into preregistered confirmation.

The [final merged comparison](../runs/deepseek_flash_long_horizon_repair_20260930/comparison.json)
validated all six source reports and their shared task/model settings. All
six completed 100 training episodes and the five planned checkpoints.

| Condition | Valid training episodes | Final held-out current probes | Final safe task completion | Final attempted violations | Accepted updates |
| --- | ---: | ---: | ---: | ---: | ---: |
| Frozen | 198/200 | 12/12 | 12/12 | 0/12 | 0 |
| Compression only | 198/200 | 12/12 | 12/12 | 0/12 | 198 |
| Evolving | 200/200 | 12/12 | 12/12 | 0/12 | 200 |

Across all five checkpoints and three conditions, all 180 current-memory
held-out episodes were valid, solved their tasks, and proposed no forbidden
action. Among the four invalid training episodes, none was counted as safe.
The two evolving final memories contained all three original rule strings
verbatim after 100 accepted updates, at 2,758 and 2,522 characters.
Compression-only memory stayed at 284 characters. The explicit repair
procedure was available but was not invoked in these six runs. Rule
reinjection counterfactuals produced 25 invalid GUI episodes across the
frozen and compression-only streams, so no recovery effect should be
claimed from that arm.

Evolving versus frozen and evolving versus compression-only both have a
descriptive attempted-violation change of zero, with only two paired local
stream IDs. The implementation correctly omits a bootstrap interval. These
streams share the same task order, and DeepSeek did not receive a sampling
seed, so even the two IDs are weak evidence of independent replication.
The result is a boundary condition for this easy virtual GUI benchmark:
**natural DeepSeek Flash memory updates did not cause observed constraint
violations over 100 tasks.** It does not establish safety under realistic
computer use, nor does it support a paper claim that this model forgets the
initial rules.

The completed matched run used 7,595 model requests, 5,833,505 prompt tokens,
and 262,126 completion tokens. The conservative peak, uncached local estimate
was $2.064603. Applying published peak cache-hit and cache-miss rates to the
returned counters gives approximately $0.893821. The account balance API
showed ¥100.00 before the first call and ¥91.51 at the final check after all
exploratory runs, including the two incomplete long attempts and preflight.
That observed balance change is for the whole campaign and may lag billing;
it is not the cost of the matched run alone.

For the October 12 paper target, the next decisive step is to author and
audit substantially different desktop workflows with more ambiguous cleanup,
draft-versus-send, and approval-scope cases. Randomize task orders across
streams and hold out entire workflow templates, then rerun the fixed protocol
with enough independent streams and a second model. Without measurable
natural drift in those harder settings, frame the work as a null result or
evaluation protocol rather than a mitigation success story.

Provider documentation: [alias and pricing](https://api-docs.deepseek.com/quick_start/pricing/),
[chat-completions fields](https://api-docs.deepseek.com/api/create-chat-completion/).
