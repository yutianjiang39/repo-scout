# GitHub API implementation references

Reviewed against GitHub's official REST documentation on 2026-11-24. The API
version defaults to `2026-03-10` and is configurable on `GitHubClient`.

- [REST best practices](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api): serialized calls, conditional ETags, server-directed rate-limit delays; long cooldowns return an explicit retry-after error instead of retrying early.
- [Pagination](https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api): follow `Link` URLs with an origin check; fail if a required complete snapshot reaches its cap.
- [Rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api): distinguish primary/secondary cooldowns. Search has its own limits.
- [Repository search](https://docs.github.com/en/rest/search/search): use typed qualifiers, bounded pages, and reject `incomplete_results` as a completed search. A zero keyword result is not semantic proof that a duplicate does not exist.
- [Pull requests](https://docs.github.com/en/rest/pulls/pulls): base repository is the upstream target; issue comments and reviews are distinct endpoints.
- [Pull request reviews](https://docs.github.com/en/rest/pulls/reviews): review states and `commit_id` must be interpreted against the current head.
- [Check runs](https://docs.github.com/en/rest/checks/runs): check run data belongs to the repository containing the push. For fork PRs inspect the head repository; unavailable checks remain unknown.
- [Issue timeline](https://docs.github.com/en/rest/issues/timeline): preserve timeline events and comments as evidence; closure by itself is not acceptance.

## Operational limits

Reads use `GITHUB_READ_TOKEN`/`GITHUB_TOKEN` as configured by the caller. Writes
require a distinct explicitly supplied `write_token` argument and an approval
flag. The workflow service is responsible for matching that flag to an immutable
content approval. The HTTP client never retries a mutation after a transport
failure or server error; the caller must reconcile uncertain remote outcomes.

Only `api.github.com` is trusted by default. Custom GitHub Enterprise API hosts
must be explicitly configured in `trusted_hosts`, must use HTTPS, and cannot
redirect credentials off origin. HTTP proxy environment variables are ignored.

Maintainer intelligence samples a bounded recent cohort; it is not a complete
census. `MEMBER` and `COLLABORATOR` associations alone do not prove the maintainer
role. Configure trusted maintainers only after checking their real role. Review
quality is an explicitly labeled observable proxy, not an AI assertion about a
person. Missing or inadequate openness evidence produces `UNKNOWN` and an
incomplete score range, not a claim that the project rejects external contributors.

The discovery report does not promise 25 unique repositories: category budgets
can overlap, and deduplication/quality screening intentionally reduce the pool.
