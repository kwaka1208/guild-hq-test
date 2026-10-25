"""Guild HQ automation.

  python3 guild.py process       # (GitHub Actions) check a newly opened issue
  python3 guild.py leaderboard   # (GitHub Actions) rebuild LEADERBOARD.md
  python3 guild.py export OUT    # (teacher) write scores.csv for grading

All state lives in issues and their labels, plus data/hints.csv, data/writing.csv and data/oss.csv.
Labels are changed only by this script or by the guild master, so students cannot fake them.

Open source contributions are counted from merged pull requests:
  - pull requests to guild-quests (QUESTS_REPO) by members
  - pull requests to another team's registered game repository
Pull requests with the label `no-points` are not counted.
"""
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = os.environ.get("GITHUB_REPOSITORY", "")
ROOT = Path(os.environ.get("GUILD_ROOT", "."))
JST = timezone(timedelta(hours=9))
POINTS = {"clear": 30, "first_bonus": [10, 5, 3], "hint_penalty": 5, "merged_pr": 60}
QUESTS_REPO = os.environ.get("QUESTS_REPO") or (f"{REPO.split('/')[0]}/guild-quests" if REPO else "")

QUEST_RE = re.compile(r"^quest-\d{2}$")
HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,19}$")
TEAM_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,29}$")
REPO_URL_RE = re.compile(r"^https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$")

LABEL_COLORS = {
    "join": "0e8a16", "member": "0e8a16", "flag": "1d76db", "cleared": "fbca04",
    "wrong": "d93f0b", "invalid": "cccccc", "rejected": "000000",
    "team-repo": "5319e7", "team-repo-ok": "5319e7",
}


# ---------- GitHub (through the gh CLI) ----------

def gh(*args, check=True):
    r = subprocess.run(["gh", *args], text=True, capture_output=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {r.stderr.strip()}")
    return r


def list_issues(label):
    """All issues (open and closed) with a label, oldest first."""
    out = gh("api", f"repos/{REPO}/issues?state=all&labels={label}&per_page=100",
             "--paginate", "--slurp").stdout
    pages = json.loads(out) if out.strip() else []
    issues = [i for page in pages for i in page if "pull_request" not in i]
    issues.sort(key=lambda i: i["created_at"])
    return issues


_known_labels = set()


def ensure_label(name):
    if name in _known_labels:
        return
    color = LABEL_COLORS.get(name, "c5def5")
    gh("label", "create", name, "--repo", REPO, "--color", color, "--force")
    _known_labels.add(name)


def reply(number, message, add=(), close=True):
    gh("issue", "comment", str(number), "--repo", REPO, "--body", message)
    for label in add:
        ensure_label(label)
    if add:
        gh("issue", "edit", str(number), "--repo", REPO, "--add-label", ",".join(add))
    if close:
        gh("issue", "close", str(number), "--repo", REPO)


# ---------- helpers ----------

def normalize(answer):
    return " ".join(answer.strip().lower().split())


def make_flag(github_id, answers):
    """Must match engine/quest.py."""
    joined = "|".join(normalize(a) for a in answers)
    digest = hashlib.sha256((github_id.strip().lower() + "|" + joined).encode("utf-8")).hexdigest()
    return "FLAG{" + digest[:10] + "}"


def parse_form(body):
    """Turn an issue form body ('### Label\\n\\nvalue') into {label: value}."""
    fields = {}
    for section in re.split(r"^### ", body or "", flags=re.M)[1:]:
        label, _, value = section.partition("\n")
        value = value.strip()
        fields[label.strip()] = "" if value == "_No response_" else value
    return fields


def roster():
    ids = re.split(r"[\s,]+", os.environ.get("GUILD_ROSTER", "").strip())
    return {i.lower() for i in ids if i}


def quest_answers():
    raw = os.environ.get("QUEST_ANSWERS", "").strip()
    return json.loads(raw) if raw else {}


def parse_time(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


# ---------- reading the state ----------

def gather():
    allowed = roster()

    def ok(login):
        return not allowed or login.lower() in allowed

    members = {}  # login -> {github_id, handle, team}; the latest Join wins
    for issue in list_issues("member"):
        login = issue["user"]["login"]
        f = parse_form(issue["body"])
        if ok(login):
            members[login.lower()] = {"github_id": login, "handle": f.get("Handle", login),
                                      "team": f.get("Team", "").lower()}

    clears = defaultdict(dict)  # login -> {quest: time of the first correct flag}
    for issue in list_issues("cleared"):
        login = issue["user"]["login"].lower()
        for label in issue["labels"]:
            q = label["name"]
            if QUEST_RE.match(q) and q not in clears[login]:
                clears[login][q] = parse_time(issue["created_at"])

    team_repos = {}  # team -> url; the latest registration wins
    for issue in list_issues("team-repo-ok"):
        f = parse_form(issue["body"])
        team_repos[f.get("Team", "").lower()] = f.get("Repository URL", "")

    return members, clears, team_repos


def merged_prs(repo):
    """Merged pull requests of a public repository, without the `no-points` label."""
    r = gh("api", f"repos/{repo}/pulls?state=closed&per_page=100", "--paginate", "--slurp", check=False)
    if r.returncode != 0:
        print(f"warning: cannot read pull requests of {repo}", file=sys.stderr)
        return []
    pages = json.loads(r.stdout) if r.stdout.strip() else []
    prs = [p for page in pages for p in page if p.get("merged_at")]
    return [p for p in prs if "no-points" not in {l["name"] for l in p.get("labels", [])}]


def gather_contributions(members, team_repos):
    """login -> list of merged pull requests that count as contributions."""
    contrib = defaultdict(list)

    def add(pr, kind):
        contrib[pr["user"]["login"].lower()].append({
            "kind": kind, "url": pr["html_url"], "title": pr["title"],
            "merged_at": parse_time(pr["merged_at"])})

    if QUESTS_REPO:
        for pr in merged_prs(QUESTS_REPO):
            if pr["user"]["login"].lower() in members:
                add(pr, "quests")
    for team, url in sorted(team_repos.items()):
        m = REPO_URL_RE.match(url)
        if not m:
            continue
        for pr in merged_prs(f"{m.group(1)}/{m.group(2)}"):
            author = members.get(pr["user"]["login"].lower())
            if author and author["team"] != team:  # only pull requests from other teams
                add(pr, "teams")
    return contrib


def read_csv(name):
    path = ROOT / "data" / name
    if not path.is_file():
        return []
    lines = [l for l in path.read_text(encoding="utf-8-sig").splitlines()
             if l.strip() and not l.lstrip().startswith("#")]
    return [{k.strip(): (v or "").strip() for k, v in r.items() if k} for r in csv.DictReader(lines)]


# ---------- process a new issue ----------

def process():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    issue = event["issue"]
    number = issue["number"]
    login = issue["user"]["login"]
    labels = {l["name"] for l in issue["labels"]}
    fields = parse_form(issue["body"])

    if labels & {"join", "flag", "team-repo"} and roster() and login.lower() not in roster():
        reply(number, f"Sorry @{login}, you are not on the guild roster. "
                      "If you are a student in this class, please tell the guild master.", ["rejected"])
        return

    if "join" in labels:
        handle = fields.get("Handle", "").strip()
        team = fields.get("Team", "").strip().lower()
        problems = []
        if not HANDLE_RE.match(handle):
            problems.append("**Handle** must be 1-20 letters, numbers, spaces, `-` or `_`.")
        if not TEAM_RE.match(team):
            problems.append("**Team** must use lowercase letters, numbers and `-` only (example: `team-a`).")
        if problems:
            reply(number, "Almost! Please fix this and open a new issue:\n\n- " + "\n- ".join(problems),
                  ["invalid"])
        else:
            reply(number, f"Welcome to the guild, **{handle}**! You are a member of **{team}**. ⚔️",
                  ["member"])
        return

    if "flag" in labels:
        members, clears, _ = gather()
        if login.lower() not in members:
            reply(number, "Please join the guild first. Use the **Join the Guild** form.", ["invalid"])
            return
        answers = quest_answers()
        quest = fields.get("Quest", "").strip().lower()
        flag = fields.get("Flag", "").strip().strip("`")
        if quest not in answers:
            known = ", ".join(f"`{q}`" for q in sorted(answers)) or "(none yet)"
            reply(number, f"I don't know that quest. Open quests: {known}", ["invalid"])
            return
        if quest in clears.get(login.lower(), {}):
            reply(number, f"You have already cleared `{quest}`. 🎉 No need to submit again.", ["invalid"])
            return
        if flag == make_flag(login, answers[quest]):
            rank = 1 + sum(1 for c in clears.values() if quest in c)
            medal = {1: " 🥇", 2: " 🥈", 3: " 🥉"}.get(rank, "")
            reply(number, f"✅ Correct! You cleared `{quest}`. "
                          f"You are adventurer **#{rank}** to clear it.{medal}", ["cleared", quest])
        else:
            if not (flag.startswith("FLAG{") and flag.endswith("}")):
                tip = "A flag looks like `FLAG{xxxxxxxxxx}`."
            else:
                tip = (f"Check that you typed your GitHub ID (`{login}`) correctly in `quest.py`, "
                       "and that you copied the whole flag.")
            reply(number, f"❌ Wrong flag for `{quest}`. {tip} Then open a new issue.", ["wrong"])
        return

    if "team-repo" in labels:
        members, _, _ = gather()
        me = members.get(login.lower())
        team = fields.get("Team", "").strip().lower()
        url = fields.get("Repository URL", "").strip()
        m = REPO_URL_RE.match(url)
        if not me:
            reply(number, "Please join the guild first. Use the **Join the Guild** form.", ["invalid"])
        elif team != me["team"]:
            reply(number, f"You joined as a member of `{me['team']}`, not `{team}`.", ["invalid"])
        elif not m:
            reply(number, "The URL should look like `https://github.com/your-id/your-game`.", ["invalid"])
        elif gh("api", f"repos/{m.group(1)}/{m.group(2)}", check=False).returncode != 0:
            reply(number, "I cannot find that repository. Is it **public**?", ["invalid"])
        else:
            reply(number, f"Registered! The repository of **{team}** is {url}", ["team-repo-ok"])
        return
    # Any other issue (questions etc.) is left for the guild master.


# ---------- scores ----------

def compute(members, clears, quest_names, hints, writing, contrib, oss):
    hint_count = defaultdict(int)
    for h in hints:
        hint_count[h["github_id"].lower()] += int(h.get("count") or 1)
    writing_pts = defaultdict(float)
    for w in writing:
        writing_pts[w["github_id"].lower()] += float(w.get("points") or 0)
    real_oss = defaultdict(list)
    for o in oss:
        real_oss[o["github_id"].lower()].append(o)

    bonus, firsts = defaultdict(int), {}
    for q in quest_names:
        timed = sorted((c[q], sid) for sid, c in clears.items() if q in c and sid in members)
        firsts[q] = [members[sid]["handle"] for _, sid in timed[:len(POINTS["first_bonus"])]]
        for (_, sid), b in zip(timed, POINTS["first_bonus"]):
            bonus[sid] += b

    rows = []
    for sid, m in members.items():
        cleared = [q for q in quest_names if q in clears.get(sid, {})]
        reading = len(cleared) * POINTS["clear"] + bonus[sid] - hint_count[sid] * POINTS["hint_penalty"]
        prs = contrib.get(sid, [])
        pr_quests = sum(1 for p in prs if p["kind"] == "quests")
        pr_teams = sum(1 for p in prs if p["kind"] == "teams")
        real = real_oss.get(sid, [])
        oss_pts = len(prs) * POINTS["merged_pr"] + sum(float(o.get("points") or 0) for o in real)
        total = reading + writing_pts[sid] + oss_pts
        rows.append({**m, "cleared": cleared, "bonus": bonus[sid], "hints": hint_count[sid],
                     "reading": reading, "writing": writing_pts[sid], "oss": oss_pts,
                     "pr_quests": pr_quests, "pr_teams": pr_teams, "real_oss": real, "total": total})
    rows.sort(key=lambda r: (-r["total"], r["handle"].lower()))
    return rows, firsts


def fmt(n):
    return str(int(n)) if float(n).is_integer() else f"{n:.1f}"


def render(rows, quest_names, firsts, team_repos, now):
    out = ["# Leaderboard", "", f"Updated: {now:%Y-%m-%d %H:%M} (JST)", ""]
    if not rows:
        return "\n".join(out + ["No members yet. Join the guild!", ""])
    out += ["## Adventurers", "",
            "| Rank | Adventurer | Team | " + "".join(f"{q} | " for q in quest_names)
            + "Reading | Writing | OSS | Total |",
            "|---:|---|---|" + "---|" * len(quest_names) + "---:|---:|---:|---:|"]
    rank, prev = 0, None
    for i, r in enumerate(rows, start=1):
        if r["total"] != prev:
            rank, prev = i, r["total"]
        marks = "".join(("⭐" if q in r["cleared"] else "·") + " | " for q in quest_names)
        out.append(f"| {rank} | {r['handle']} | {r['team']} | {marks}"
                   f"{fmt(r['reading'])} | {fmt(r['writing'])} | {fmt(r['oss'])} | **{fmt(r['total'])}** |")

    teams = defaultdict(list)
    for r in rows:
        teams[r["team"]].append(r["total"])
    out += ["", "## Teams", "", "Average points per member.", "",
            "| Rank | Team | Members | Average | Game |", "|---:|---|---:|---:|---|"]
    ranked = sorted(teams.items(), key=lambda kv: (-sum(kv[1]) / len(kv[1]), kv[0]))
    for i, (team, totals) in enumerate(ranked, start=1):
        url = team_repos.get(team, "")
        game = f"[repository]({url})" if url else ""
        out.append(f"| {i} | {team} | {len(totals)} | {fmt(sum(totals) / len(totals))} | {game} |")

    if any(firsts.values()):
        out += ["", "## First to clear", "", "| Quest | 🥇 | 🥈 | 🥉 |", "|---|---|---|---|"]
        for q in quest_names:
            names = (firsts[q] + ["", "", ""])[:3]
            out.append(f"| {q} | " + " | ".join(names) + " |")

    contributors = [r for r in rows if r["pr_quests"] or r["pr_teams"] or r["real_oss"]]
    if contributors:
        contributors.sort(key=lambda r: (-r["oss"], r["handle"].lower()))
        out += ["", "## Contributors", "",
                f"Merged pull requests. Each one is worth {POINTS['merged_pr']} points.", "",
                "| Adventurer | guild-quests | Other teams' games | Real OSS |", "|---|---:|---:|---:|"]
        for r in contributors:
            out.append(f"| {r['handle']} | {r['pr_quests']} | {r['pr_teams']} | {len(r['real_oss'])} |")

    fame = [(r["handle"], o) for r in rows for o in r["real_oss"]]
    if fame:
        out += ["", "## Hall of Fame", "", "Contributions to real open source projects. 🏆", ""]
        for handle, o in fame:
            out.append(f"- **{handle}**: {o.get('url', '')}")
    return "\n".join(out) + "\n"


def quest_names_for(clears):
    names = set(quest_answers())
    for c in clears.values():
        names |= set(c)
    return sorted(names)


def scores():
    members, clears, team_repos = gather()
    names = quest_names_for(clears)
    contrib = gather_contributions(members, team_repos)
    rows, firsts = compute(members, clears, names, read_csv("hints.csv"), read_csv("writing.csv"),
                           contrib, read_csv("oss.csv"))
    return rows, firsts, names, team_repos


def leaderboard():
    rows, firsts, names, team_repos = scores()
    md = render(rows, names, firsts, team_repos, datetime.now(JST))
    path = ROOT / "LEADERBOARD.md"
    old = path.read_text(encoding="utf-8") if path.is_file() else ""

    def body(text):  # ignore the "Updated:" line so that nothing is committed when nothing changed
        return [l for l in text.splitlines() if not l.startswith("Updated: ")]

    if body(old) == body(md):
        print("No changes in the leaderboard.")
        return
    path.write_text(md, encoding="utf-8")
    print(md)


def export(out_path):
    rows, _, names, _ = scores()
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["github_id", "handle", "team", *names, "first_bonus", "hints",
                    "pr_quests", "pr_teams", "real_oss", "reading", "writing", "oss", "total"])
        for r in sorted(rows, key=lambda r: r["github_id"].lower()):
            w.writerow([r["github_id"], r["handle"], r["team"],
                        *[1 if q in r["cleared"] else 0 for q in names],
                        r["bonus"], r["hints"], r["pr_quests"], r["pr_teams"], len(r["real_oss"]),
                        fmt(r["reading"]), fmt(r["writing"]), fmt(r["oss"]), fmt(r["total"])])
    print(f"Wrote {out_path} ({len(rows)} members)")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "process":
        process()
    elif command == "leaderboard":
        leaderboard()
    elif command == "export" and len(sys.argv) > 2:
        export(sys.argv[2])
    else:
        sys.exit(__doc__)
