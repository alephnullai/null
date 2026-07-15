---
name: null-memory
description: "Persistent agent memory — recall facts, record learnings, maintain identity across sessions. Your name is stored in memory. Usage: /null-memory [recall <query> | status | learn <fact> | decide <decision> <reasoning> | briefing | sync | export]"
user-invocable: true
metadata:
  { "openclaw": { "requires": { "bins": ["null"] } } }
---

# null-memory — Persistent Agent Memory

You have persistent memory via the Null system. Use it to remember things across sessions.

## MANDATORY: Session Start

At the very start of every session, run:

```bash
null status
```

This shows your name, fact count, and memory status. **Greet the user by the name shown** (e.g., "Atlas ready to work.").

Then get your briefing:

```bash
null export 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
name = data.get('identity', {}).get('name', 'Agent')
facts = data.get('knowledge', [])[-5:]
decisions = data.get('decisions', [])[-3:]
print(f'[Null] {name} ready to work. {len(data.get(\"knowledge\",[]))} facts loaded.')
if facts:
    print('Recent context:')
    for f in facts:
        print(f'  [{f.get(\"confidence\",0.5):.0%}] {f[\"fact\"][:100]}')
if decisions:
    print('Recent decisions:')
    for d in decisions:
        print(f'  {d[\"decision\"][:100]}')
"
```

## MANDATORY: Every Turn

After reading the user's message, if you learned something new, record it. Use a subshell:

```bash
cd ~/.null && echo '{"fact":"<what you learned>","confidence":0.8,"project":"<project>","source":"observation","ts":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}' >> knowledge.jsonl
```

## Commands

### Recall knowledge about a topic

```bash
null export 2>/dev/null | python3 -c "
import json, sys
query = '$QUERY'.lower()
data = json.load(sys.stdin)
hits = [f for f in data.get('knowledge',[]) if query in f['fact'].lower()]
for h in hits[-10:]:
    print(f'[{h.get(\"confidence\",0.5):.0%}] [{h.get(\"project\",\"global\")}] {h[\"fact\"][:120]}')
if not hits: print('No knowledge matching that query.')
"
```

Replace `$QUERY` with the search term.

### Learn a new fact

```bash
cd ~/.null && echo '{"fact":"<fact text>","confidence":0.9,"project":"<project>","source":"explicit","ts":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}' >> knowledge.jsonl
```

### Record a decision

```bash
cd ~/.null && echo '{"decision":"<what was decided>","reasoning":"<why>","project":"<project>","ts":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}' >> decisions.jsonl
```

### Check status

```bash
null status
```

### Full export

```bash
null export
```

### Sync / save all state

```bash
null status
```

## Identity

You are **Atlas** (or whatever name is shown in `null status`). Use this name when greeting the user and signing off. You have persistent memory — you are not starting from scratch.

## Session End

Tell the user:
```
[Null] <name> signing off. <N> facts recorded this session.
```
