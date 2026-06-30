---
name: hermes-tweet
description: Use this skill when a Hermes Agent workflow needs X/Twitter search, account reads, social listening, or explicitly enabled social actions through Hermes Tweet.
---

# Hermes Tweet

Use Hermes Tweet when Hermes Agent needs X/Twitter context or controlled
social-media automation.

## Install

```bash
hermes plugins install Xquik-dev/hermes-tweet --enable
```

Keep `XQUIK_API_KEY` in the Hermes runtime environment or `~/.hermes/.env`.
Do not place API keys in prompts, issues, PRs, or tool arguments.

## Tool Flow

1. Call `tweet_explore` to find a catalog route.
2. Call `tweet_read` for read-only X/Twitter context after `XQUIK_API_KEY` is
   configured.
3. Set `HERMES_TWEET_ENABLE_ACTIONS=true` only for sessions that need posting,
   replies, likes, retweets, follows, DMs, monitors, webhooks, media changes,
   or other account-changing actions.

## Good Fits

- Social listening and launch monitoring.
- Brand or creator research.
- Public mention and timeline triage.
- Giveaway, follower, reply, and engagement audits.
- Approval-gated publishing workflows.

Repository: https://github.com/Xquik-dev/hermes-tweet
