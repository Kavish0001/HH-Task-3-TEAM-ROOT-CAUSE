# Demo recording script

A plain screen recording, roughly **4 to 5 minutes**. No editing needed. Everything
below is real output from this machine — no figure in this script is invented, so if
what you see on screen differs, read out what you actually see.

---

## Before you hit record

Run this checklist. Two of these have bitten us already.

```bash
# 1. Is the chain node up? Must print a block number, not an error.
python -c "import requests;print(requests.post('http://127.0.0.1:8545',json={'jsonrpc':'2.0','method':'eth_blockNumber','id':1},timeout=5).json())"

# 2. If it is NOT up, start it and redeploy (two terminals):
cd chain && npx hardhat node          # terminal 1, leave running
npm --prefix chain run deploy         # terminal 2

# 3. Is the web UI up?
python web/app.py                     # -> http://127.0.0.1:5000
```

**Do not restart the Hardhat node after you start recording.** Its state is in memory.
Restarting it wipes every anchored proof, and your `--verify` step will correctly but
embarrassingly report that nothing was ever anchored.

**Close other tabs and set the browser to 1280×800 or larger.** The UI is laid out for
that and the three stage cards should all be visible at once.

**Have a second terminal ready** in the repo root for the verify/tamper section.

---

## Shot 1 — the landing page (~30 s)

Open `http://127.0.0.1:5000`.

> "This is FaceChain. It takes a face scan, searches the live web and social media for
> a matching post, and then anchors what it found on a blockchain so the result can be
> re-verified later and can't be quietly edited afterwards."

Scroll down through the sections. Pause on the negative control.

> "The number I'd point at first is this one. When we search Messi's face against the
> candidate pool built for Elon Musk, twenty-four faces get encoded and compared, and
> nothing matches — the best score is 0.185 against a threshold of 0.363. The matcher
> isn't just accepting whatever the search engine hands it."

Scroll back up, click **RUN THE PIPELINE**.

---

## Shot 2 — stage 1, the face (~30 s)

You're on `/app` now.

Click the **Elon Musk** sample thumbnail. The hint field fills in automatically.

> "I'll use a bundled sample — a freely-licensed portrait from Wikimedia Commons. You
> can also drop in your own photo."

Click **RUN PIPELINE**.

Stage 1 fills in almost immediately. Point at the box drawn over the face.

> "YuNet finds the face, SFace encodes it into a 128-dimensional vector. That vector is
> the only form of the face anything downstream compares — this hash here is its
> fingerprint. Nothing has touched the network yet."

---

## Shot 3 — stage 2, the search (~60–90 s)

This is the part that takes real time. Let it run and talk over it.

> "Now it queries six live providers — Mastodon, Reddit, LinkedIn, DuckDuckGo and
> Wikimedia Commons, plus X. Every candidate image it finds gets downloaded and
> re-encoded, and compared to that embedding."

As candidate rows stream in with their similarity bars:

> "It never trusts a caption. A page can say 'Elon Musk' all it likes — the only thing
> that decides a match is the distance between the two embeddings. Anything under 0.363
> is discarded."

Point at the **ABOVE THRESHOLD** counter (the one with the cyan bar).

When the best match resolves, hover or point at the winning row.

> "That's the match — a real post, found by a live query. That URL appears nowhere in
> the source code."

**If a higher-scoring news result sits above the chosen one**, say so — it's in the
audit trail anyway and volunteering it is stronger than being asked:

> "You'll notice a news site actually scored higher. The pipeline deliberately prefers a
> social-media post for the headline result, since that's what the brief asks for, and
> it records that choice in the notes rather than hiding it."

---

## Shot 4 — stage 3, the chain (~45 s)

> "The findings get reduced to a small canonical record — the image hash, the embedding
> fingerprint, the post URL, the similarity and the threshold — serialised one fixed way
> and hashed."

Point at the record hash, then the receipt.

> "That digest goes to a Solidity contract on a local Hardhat chain. Real transaction,
> real block, real gas. What's on chain is a commitment, not a copy — no face, no image,
> no personal data is published, only the hash."

Then the two verdict rows.

> "Re-verification recomputes the digest from the evidence and compares it against what
> it reads back off the chain. They match. Then it changes one field — one query string
> on the post URL — and the digest is completely different. The chain rejects it."

---

## Shot 5 — the conclusion report (~20 s)

Click **DOWNLOAD CONCLUSION REPORT (PDF)** and open it.

> "Every run writes this. It's the same evidence in plain English rather than JSON —
> what was submitted, what the search actually did, what was found and by what margin,
> what went on chain, and both digests from the tamper test. Three pages, generated
> automatically."

Scroll through it briefly. The two thumbnails side by side are worth pausing on.

---

## Shot 6 — the command line (~45 s)

Switch to the terminal. This proves the UI isn't the product — the pipeline is.

```bash
python run_pipeline.py --verify out/proof-<id>.json
```

Use the proof filename printed at the end of the run (or `ls out/proof-*.json`).

> "The same check from the command line, against a saved proof. Verified."

```bash
python run_pipeline.py --verify out/proof-<id>.json --tamper
```

> "And the same proof with one field altered. Tamper detected. That's the whole point of
> anchoring it."

Optional, if you have time — the zero-setup path:

```bash
python run_pipeline.py --image samples/sundar-pichai.jpg --hint "Sundar Pichai" --chain sim
```

> "There's also a local proof-of-work chain built in, so the whole thing runs with no
> Node and no chain node at all."

---

## Shot 7 — close (~20 s)

Back to the repo or the landing page footer.

> "Source is on GitHub with a README covering the setup, the contract, and the
> limitations — including the ones that aren't flattering. It's a research demo using
> public figures and freely-licensed images, not a surveillance tool."

Done.

---

## If something goes wrong mid-recording

**"no post cleared the identity threshold"** — DuckDuckGo rate-limits in bursts and a
run can come back empty. Don't panic on camera; just say the search came back empty and
run it again. It's an honest outcome and the counters prove it looked. Re-running almost
always fixes it.

**A stage card shows an error** — the message renders in the card. Read it out and move
on to the terminal section; the CLI path is independent of the UI.

**The chain shows `simchain` instead of `evm-hardhat`** — the Hardhat node isn't
reachable and it fell back. That's designed behaviour, so either say so and continue, or
stop, start the node, redeploy, and restart the recording.

**Verify says nothing was anchored under that record id** — the node was restarted
between anchoring and verifying. Re-run the pipeline to create a fresh proof, then verify
that one.

---

## Things worth saying because they're true and most demos can't say them

- The matching post URL comes out of a live query. Grep the repo for it; it isn't there.
- The negative control: a wrong face scores 0.185 where a right face scores 0.74–0.78.
- Reddit blocks unauthenticated JSON with a 403, so it goes through a public redlib
  front-end — the permalinks and `i.redd.it` image URLs are still genuine Reddit URLs.
- X contributes nothing. It strips Open Graph from status pages, and its avatars often
  aren't faces. The run says so in the notes rather than pretending otherwise.
- LinkedIn does work: a profile photo matched the Pichai sample at cosine 0.7365, and a
  colleague's profile that came back in the same search was correctly rejected by the
  face check.
