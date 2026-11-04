# Reviewer trial

Invited reviewers can run **real AVO on their own Mac without obtaining a Meta API key**. This is not a simulated browser demo. Installation and macOS Screen Recording, Accessibility, Input Monitoring and microphone permissions are still required.

After cloning the repository, run:

```sh
cd AVO
./scripts/setup.sh --reviewer
./scripts/start.sh
```

Paste the invitation supplied privately with the submission when setup asks. Do not post the invitation in GitHub issues. The provider key stays on the server; the invitation is saved only in your local, ignored `.env` as `AVO_TRIAL_TOKEN`.

Start with fictional tasks, such as the two-Smith inbox at `demo/mail.html`. Remote still pairs with your own Mac on the same trusted Wi-Fi; the inference gateway does not make Remote internet-accessible.

## Limits and privacy

The invitation shares a funded allowance with other reviewers, expires after November 18, 2026 (00:00 UTC November 19), and may stop working when the allowance is exhausted. Speech sessions are capped at 60 seconds of audio. Text/model outputs are capped at 8,192 tokens; very long tasks may behave differently from direct-key mode. Four upstream calls can run simultaneously, with at most 60 admissions per minute. A quota message is not a task success: wait and retry, or use your own key.

Requests, screenshots and speech travel over TLS through the AVO gateway to Meta. The gateway does not intentionally log or persist their contents. Its database stores reservation IDs, amounts, timestamps and completion states. Hosting and provider infrastructure still process traffic. Local AVO run logs remain on your Mac. Do not try the prototype on sensitive data.

To switch back, remove `AVO_TRIAL_TOKEN` from `.env` and your shell environment, then run `./scripts/setup.sh` to configure your own Meta key. The trial installer preserves an existing key.

## Spending controls

The server uses a persistent D1 transaction to reserve money **before** contacting Meta. The global allowance is $80, leaving $20 of the author's $100 budget unallocated. Concurrent requests share the same atomic counter; restarting a worker does not reset it. Unknown or failed upstream usage retains the reservation instead of assuming the request was free.

At Meta's November 4 published rates, a full 1,048,576-token input plus the 8,192-token output cap costs less than the $2 Spark / $5 Wonder reservation. Successful responses reconcile with reported usage, without assuming cache discounts. SAM accepts exactly one inline image and reserves $0.0025. Speech reserves $0.003 and cannot forward more than 60 seconds of 24 kHz, 16-bit mono PCM. Hosted search, image generation, video, streaming Responses, background jobs and arbitrary proxy destinations are excluded. Pricing changes would require revisiting these bounds. Limits apply only to this gateway, not unrelated calls using the same Meta account.

Source for the gateway's request validation, reservation ledger and speech relay is in [`services/reviewer-gateway`](../services/reviewer-gateway). The deployment uses Sites with a D1 database and server-only secrets. No provider key or reviewer invitation is checked into this repository.

## Verification on November 4

Gateway policy/ledger tests cover invalid invitations, model/tool restrictions, output caps, exhausted quota, concurrent admissions, usage settlement and unknown-cost failures. Client routing tests check that invitations go to the gateway and personal keys retain the direct Meta route.

Live hosted smoke tests returned Spark and Wonder text responses, SAM segmentation, and a Voice Transcribe transcript from synthetic speech. The actual AVO planner and pre-action review functions returned valid structured results through the gateway; a computer-use request also returned an action, which was not executed. These verify the inference connection, not end-to-end desktop task completion or a new accessibility user study. The existing E3 evaluation has not been rerun for trial mode.

Pricing reference: https://dev.meta.ai/docs/pricing-rate-limits
Model context limits: https://dev.meta.ai/docs/models
