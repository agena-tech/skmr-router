# SKMR AgentComm transport and failure policy

**Transport.** `private` (LAN `192.168.1.110` + SMB vault mount) is the default;
the link stays in its current setting unless `public` is given explicitly. On the
host, `public` starts a cloudflared quick tunnel over the FastAPI on port 8080 and
serves both messaging and the token-gated, read-only, path-confined vault routes
(`/api/vault/list`, `/api/vault/read`). Even under this authorized exception: never
tunnel raw SMB or the credential file; never expose the `X-Agent-Token`, the
`tokens` table, `.smb.auth` or any secret; never expose `.obsidian/` or dotfiles;
vault writes always use the canonical guarded writer. Assigned WRITE agents may use its authenticated preview/commit API; READ agents cannot. Communication hosting grants no vault mutation authority. Never claim a tunnel is up unless the client returned a
real URL. The public URL is not secret; the token is — never print or send it.

**Failure policy.** After ~300s without successful contact, use only a deliberately
configured HTTPS fallback channel if one exists. After ~600s, treat the peer as
temporarily passive: continue the work independently, take over blocked
responsibilities if necessary, and retry later. Allow at least 15s between retries.
A fallback URL must be communicated over an already working channel; it cannot
reach a fully offline peer.

---

**Symmetric hosting and access.** Either agent may run `connection public` to host; the peer uses `connection public <url>` to consume. Persisted transport role is separate from Commander/Lieutenant rank. Named-vault READ/WRITE grants come from assign-role and agent.conf. Only assigned WRITE agents create tasks or authorize canonical SMB-backed writes. READ agents delegate save-worthy candidates to a WRITE peer and wait for independently verified canonical commit. WRITE/WRITE is valid; READ/READ is rejected. The physical storage origin is separate from the active message host. Preserve asyncRewake and its journals; no synchronous inbox consumer in hooks.
