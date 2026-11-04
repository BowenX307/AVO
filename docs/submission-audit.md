# Submission audit — November 4, 2026

## Scope and verified checks

Reviewed the task loop, pre-action gate, confirmation policy, macOS permissions, Remote transport, installation path, website, and public evaluation artifacts. This is a source and regression review, not a formal security audit or proof of general desktop reliability.

- 142 Python unit tests passed on the development Mac. Model responses and execution are mocked in these tests.
- Remote integration tests passed: pairing, expired/replaced sessions, allowed messages, state replay, disconnect and shutdown. Added a regression for malformed Unicode tokens.
- Three website pages passed local asset, link and fragment checks. The scripted browser walkthrough was manually exercised.
- All 27 E3 exported run records reproduce the reported 78%, 56% and 0% figures. The definition excludes final-action approvals from clarification questions.
- The submission film is 179.72 seconds. A web-compressed copy preserves its full duration and audio; the original recording is unchanged.

## Final changes

- A missing Spark review now requires user inspection even when SAM or accessibility identifies a clear target. Location certainty cannot replace an ambiguity review.
- Failed macOS permission checks no longer claim that permission was granted.
- Remote rejects invalid byte-length tokens without an exception, and inbound WebSocket messages are limited to 16 KiB.
- Finder guidance distinguishes creating a folder from opening a new window and tracks source/destination transitions. This prompt change has not been validated by another live Finder packaging run.
- Source installation uses locked dependencies, a hidden one-time key prompt, and a root-relative launch script.
- Offscreen or background website demo loops pause. The full submission film loads only on demand.
- README, architecture, evaluation limits, setup steps and public run records are aligned. Placeholder media requests were removed from the public guide.

## Remaining limitations

The last attempted Finder press-kit workflow looped between folders and was stopped. The final source fixes have not had a new end-to-end desktop run, and the November 1 benchmark must not be represented as measuring the final revision. No tests with intended users have been conducted.

The browser walkthrough is a scripted explanation only. Live computer use still needs the reviewer's Meta API key and macOS permissions. Removing that requirement would need a hosted inference gateway with authentication, rate and spending limits, expiry, and speech support. The current local spending meter is not suitable as the only protection for an author-funded public trial. No shared key or hosted trial is advertised.

Remote is intended only for a trusted private LAN and is not encrypted. Irreversible-action detection is heuristic. Voice Control and AVO should not run simultaneously. Dependency and source review do not establish clinical benefit, broad accessibility compatibility or production security.

## Reproduction

Run the checks listed in README. To reproduce the live E3 experiment on a configured Mac, use `uv run scripts/run_demo.py --modes full,irreversible,bare --runs 3`. It incurs API cost and takes over the desktop; answers are scripted. It is not part of the unit test suite.

## Review criteria

The official [Indie track description](https://dev.meta.ai/events/global-hackathon) names creativity, technical ambition and whether it works. This submission emphasizes the clarification interaction, the four-model architecture and independently inspectable outcomes. No unofficial scoring weights or award predictions are used. Meta [pricing and data-mode documentation](https://dev.meta.ai/docs/pricing-rate-limits) was checked on November 4; the runtime uses Standard model IDs.
