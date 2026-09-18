# tariffrag

Grounded question answering over ISO-NE and NYISO tariffs and market manuals.

Ask *"How long is a Capacity Supply Obligation for new capacity, and what terminates it?"* and get an answer where **every substantive sentence resolves to a specific tariff section, page, and verbatim quoted span** — not a paraphrase with a section number appended by a language model.

> **Status: early.** The project scaffold, data model, and tooling are in place. Ingestion, retrieval, and answering are being built in the phases listed below. This README documents the design; the checklist marks what actually exists.

## Why this is harder than a generic RAG demo

Wholesale electricity market rules are an unusually hostile corpus, and most of the design follows from that:

- **Section numbers carry no semantic signal.** `III.13.1.1.2.2` embeds near every other section number and tokenizes into fragments. Dense retrieval cannot find it; BM25 can. Hence hybrid retrieval, not as a checkbox but because half the real queries are lookups.
- **Defined Terms are exact and load-bearing.** "Capacity Supply Obligation" is not "capacity obligation" is not "Installed Capacity Requirement". Conflating them is a correctness bug — and conflating near-synonyms is precisely what embeddings are good at.
- **Statutory boilerplate compresses the embedding space.** Every section says *"shall be determined in accordance with"* and *"notwithstanding the foregoing"*, so pairwise similarity across unrelated sections is uniformly high.
- **Structure is the meaning.** A clause means what it means because of the section it sits in. Fixed-window chunking destroys exactly the signal that matters.
- **Being confidently wrong is the failure mode that counts.** An ungrounded answer about a capacity obligation is worse than no answer, so abstention is a measured metric rather than an afterthought.

## Grounding

Citations use Claude's native [`search_result` content blocks](https://platform.claude.com/docs/en/build-with-claude/search-results) rather than asking the model to emit citations as JSON or markdown footnotes.

This matters more than it sounds. Citations come back as `search_result_location` carrying `search_result_index`, `start_block_index`, `end_block_index`, and `cited_text` — and **`cited_text` is guaranteed to equal `content[start_block_index:end_block_index]` joined**. So resolving a citation to a document, section, page and span is two array lookups, not fuzzy string matching against extracted PDF text. A model cannot hallucinate a span index the way it can hallucinate a section number in prose.

That guarantee also yields a free correctness invariant: if returned `cited_text` ever disagrees with the blocks that were sent, *our own* block bookkeeping is off by one. Every answer is checked against it.

Two API constraints shape the rest: citations must be enabled on all blocks or none, and citations cannot be combined with structured outputs (`output_config.format` returns a 400). The answering call therefore uses no structured outputs; the evaluation judge, which sends no citations, does.

Every rendered citation reads `Doc Title (Rev. 27, eff. 2023-04-06) § III.13.1.2.3, p. 412`, and every answer closes by naming the corpus snapshot it was grounded in.

## Evaluation

The evaluation harness is the part that makes this a project rather than a demo. Three layers, reported separately:

**Layer 1 — retrieval.** Zero API calls, deterministic, runs in CI. `recall@k` and `MRR@10`, plus two metrics generic harnesses lack: `section_recall@k` (the section is the right unit — a section split into six chunks should not score six times) and `all_required_sections_recall@k` for multi-hop questions, reported on its own line because averaging it in makes multi-hop improvements vanish. Broken down per topic and per ISO, since a single aggregate hides being strong on capacity and weak on congestion.

**Layer 2 — answer quality.** Deterministic checks first and free: cited sections exist, cited text verifies verbatim against the canonical source, numbers in the answer appear in the cited blocks, and `iso_attribution_error` (a sentence naming NYISO while citing only ISO-NE sections — the most dangerous failure in a two-ISO corpus). Then an LLM judge that sees **only a claim and its cited span** — never the corpus, never the retrieved set — so it grades entailment rather than recall. The judge is validated against a hand-labeled subset with agreement reported, and run-to-run variance is published alongside the score.

**Layer 3 — abstention.** Abstention rate on unanswerable questions *and* false-abstention rate on answerable ones, always together, since abstaining on everything scores perfectly on one. The real failure isn't a clean refusal but a hedge, so an answer counts as a correct abstention only if it explicitly says the corpus lacks the answer, cites at most one section, and makes no substantive claim.

The **ablation table** — {lexical, dense, RRF, +rerank} × {glossary expansion} × {cross-reference expansion} × {embedding model} — is a first-class output. Features that don't move a number get cut, and the table keeps the row showing they didn't.

The gold set is hand-verified. Candidate questions are drafted from sections while reading them, but every question, answer key, and gold section id is checked by hand before it enters the set, and `provenance` is recorded per item. LLM-generated gold sets produce questions the retriever already answers and section ids that are subtly wrong.

## Architecture

Three units are kept deliberately distinct:

| Unit | Role | Boundary rule |
|---|---|---|
| `Section` | parse unit | The document's own numbered hierarchy |
| `Chunk` | retrieval unit | 400–900 tokens, never crossing a parent section |
| `CitationBlock` | citation unit | One paragraph or enumerated item |

Block size is the citation-precision knob: one oversized block produces citations meaning "somewhere in these 1200 tokens".

Parent context reaches three places for three different reasons — prepended to embedding text (recall), a separately-weighted FTS5 column (concatenating it into the body makes every chunk under `III.13` match "capacity" and saturates BM25), and the `search_result.title`, which Claude can see but **cannot cite**, so breadcrumbs never get quoted as though they were tariff text.

Storage is a single SQLite file holding metadata, FTS5, and `sqlite-vec` vectors together, so retrieval is one SQL join rather than cross-store id reconciliation. Brute-force KNN over this corpus is a few milliseconds; ANN would be complexity with no payoff.

Section headers are detected by scoring four independent signal families — numbering patterns, typography, page position, and global sequence monotonicity — because a single regex cannot tell a heading from an inline cross-reference like *"as defined in Section III.12.2"*, which is the dominant failure mode.

## Deliberately not used

LangChain / LlamaIndex (they would hide chunking, fusion, and citation mapping — the three things that are the actual work here), any vector database requiring a server, GraphRAG, fine-tuning, HyDE, multi-query fan-out, agentic retrieval loops, chunk overlap, and model-generated JSON citations.

`docling` and `unstructured` were evaluated and rejected: they pull hundreds of megabytes of layout models whose heading detection is trained on papers and reports, when in a numbered legal tariff *the numbering is the signal*.

## Scope

**In now:** ISO-NE Market Rule 1 and its appendices, at a single point-in-time snapshot (see [Corpus](#corpus)).

**In later:** ISO-NE manuals (M-11, M-20, M-28), then NYISO MST/OATT and manuals (11, 12, ICAP 4) for cross-ISO comparison.

**Out, on purpose:** multi-version corpora and time-travel queries, section-level revision diffing, FERC order tracking, per-section effective-date indexing. In their place, queries carrying temporal cues ("in 2019", "used to", "prior to") are detected and abstained on, naming the snapshot actually held.

## Phases

- [x] **0a** Project scaffold, data model, lint/type/test tooling, CI
- [x] **0b** Parser spike, validated against the real Market Rule 1 corpus
- [x] **0c** Content-hashed manifest, with per-section effective dates and docket numbers
- [ ] **1** Extraction → canonical text + page map → section tree → parse report
- [ ] **2** Chunker, FTS5, embeddings, sqlite-vec, weighted RRF
- [ ] **3** First ~30 gold questions and the Layer-1 retrieval eval
- [ ] **4** `search_result` assembly, the answer call, grounding verification
- [ ] **5** Full eval harness; gold set to ~120; judge validation
- [ ] **6** Glossary and cross-reference expansion, reranking — each with its ablation row
- [ ] **7** ISO-NE manuals and the NYISO corpus (needs the fetcher); cross-ISO comparison queries
- [ ] **8** FastAPI + web UI with clickable citation highlighting

Evaluation (phase 3) deliberately precedes answering (phase 4), so retrieval is tuned against evidence rather than vibes. The UI is last: a UI built before the eval is how these projects end up looking impressive and being wrong.

## Corpus

`corpus/isone/mr1/` holds the complete **ISO-NE Market Rule 1** — Section III of the Transmission, Markets and Services Tariff: sections 1–12, 13–14, 14, 15, and appendices A–L. 16 PDFs, 7.3 MB, 806 pages, committed so the repo is clone-and-run.

Four appendices (B, E, H, J) are `[RESERVED]` placeholders and are excluded. Nothing is dropped for being unreadable — every substantive document has a usable text layer.

## The manifest

`corpus/manifest.yaml` is the reproducibility contract: per document an identity, a title and where that title came from, a content hash, the pages each effective-date stamp governs, and whether the document takes part in the corpus at all.

```bash
tariffrag manifest build   # probe every PDF, write the manifest
tariffrag manifest show    # render it as a table
tariffrag manifest diff    # compare disk against the record; non-zero on drift
```

Three properties are deliberate:

**Rebuilds are byte-stable.** `retrieved_at` carries over for any document whose content hash is unchanged. A manifest that produced a diff on every build would stop being a record of change.

**Nothing is invented.** These documents were supplied rather than fetched, so every `url` is `null` — a plausible-looking URL nobody verified would put a false claim into the provenance chain that citations rest on. Appendices C, D and G carry no effective-date stamp anywhere, so they record `unknown` rather than a guess.

**Effective dates are per section, not per document.** Sections 13–14 carry four stamps covering pages `6-102`, `4-5,103-182,232-235`, `206-230` and `1-3`. `Document.cite_label(page=N)` resolves the stamp governing that page, so a citation to page 1 reports March 2026 while the document's primary date is May 2025.

Status is machine-readable (`active` / `reserved` / `excluded`), so Phase 1 skips the four reserved appendices without a hard-coded list. `reserved` is kept distinct from `excluded` for the same reason `EMPTY` is distinct from `NO_GO`: one loses nothing, the other loses content.

## The parser spike

The section-structure parser is the highest-risk component: chunk quality, breadcrumbs, gold section ids, cross-reference resolution and citation rendering all assume `III.13.1.2.3` boundaries are recoverable from PDF text. `tariffrag spike` measures whether that holds, per document, before any of it gets built.

```bash
tariffrag spike corpus/isone/mr1/*.pdf --show-candidates 40
```

It returns **GO / DEGRADED / EMPTY / NO_GO** per document and exits non-zero on `NO_GO`. `EMPTY` is deliberately distinct from `NO_GO`: an intentionally blank tariff section has nothing to recover, and conflating it with an unreadable scan sends you hunting for an OCR fix that cannot exist.

### Results on Market Rule 1

| | |
|---|---|
| Headings recovered | **1,216** across 806 pages |
| Sequence violations | **0** |
| Cross-references misread as headings | **0** (from 836 inline references in Sections 13–14 alone) |
| Verdicts | 7 GO, 5 DEGRADED, 4 EMPTY, 0 NO_GO |

Deepest hierarchy recovered is seven levels (`III.13.1.4.1.1.2.6`). The seven remaining numbering gaps are real — `III.13.7.1.2` simply does not exist in the tariff.

### What measurement changed

Every one of these was found by running against real text, and each had been wrong in a way that reasoning alone did not catch:

**Title-casing is a hard gate, not a weighted signal.** Wrapping puts `III.14 shall be construed to limit...` at the start of a line. Scored as one weak signal short, such lines passed at 0.75 and polluted the tree.

**Sequence demotion needs a longest increasing subsequence, not a greedy walk.** A greedy monotonic walk lets one bad acceptance set an unreachable watermark that demotes every legitimate heading after it. In Appendix A the running `Appendix A` header sorted above every `III.A.x` id and cost **180 real headings**. An LIS drops the outlier instead of the tail.

**Numbering schemes must be compared separately.** `Appendix A` and `III.A.1` have incomparable sort keys; validating them as one sequence manufactures violations that are artefacts of the comparison.

**Page furniture is positional, low-variance, and recurrent — all three.** Text matching misses ISO-NE footers, which carry a per-page effective date and so take several textual forms. Exact position misses them too, because the footer drifts between bands (710.5 on 97 pages, 709.2 on 83, 734.7 on 23). Position alone over-matches, flagging the first body line of every page. Only the conjunction works.

**Reserved subsections are real tree nodes.** ISO-NE repeals in place, leaving `III.13.1.1.2.5.2. [Reserved.]`. `[` is not uppercase, so the title gate rejected them — manufacturing seven false gaps.

**Font subset prefixes fragment font identity.** The same face appears as `CPJYEE+TimesNewRomanPSMT` and `MEJGOK+TimesNewRomanPSMT`; the tag must be stripped before comparison.

**Contents pages mimic headings perfectly.** Sections 1–12 open with a genuine 33-page table of contents listing every section in Market Rule 1. Detected by id density per page rather than by the literal phrase.

### A finding that changed the design

Every page carries `Effective Date: 3/31/26 – Docket No. ER26-925-000`, and **the dates differ within a single document** — Sections 13–14 contain four, covering 97, 86, 25 and 3 pages. ISO-NE versions at finer granularity than the file, so effective dates and FERC docket numbers are captured per section rather than per document. That is richer than the per-document versioning originally planned.

### Fixtures

`tests/fixtures/` holds four synthetic PDFs, each encoding one hazard in isolation — deep nesting, the cross-reference trap, headings set in body type, and an image-only scan. They are committed and byte-deterministic (regenerate with `python tests/fixtures/make_fixtures.py`).

They prove the detector reacts correctly to a hazard when present; only the real corpus shows which hazards actually occur. Both layers are in the test suite.

One fixture result is worth stating, because it is the load-bearing claim of the cascade: the flat-typography fixture still recovers **every** heading. Typography is a bonus signal; numbering, the prose gate, and sequence consistency carry the work.

## Development

```bash
uv venv && uv pip install -e ".[dev]"

ruff check . && ruff format --check .
mypy
pytest
```

Local embedding and reranking models live in the `local-models` extra (`uv pip install -e ".[dev,local-models]"`), since they pull torch and sit behind the `Embedder` / `Reranker` protocols.

### Corpus and the index

`corpus/manifest.yaml` and the Market Rule 1 PDFs are committed; the built index is not — `tariffrag ingest` rebuilds it reproducibly from the manifest, keyed by content hash.

Committing 7.3 MB of source PDFs is a deliberate reversal of the usual rule. It makes the repo clone-and-run, which is worth more here than repo slimness. If the manuals and NYISO push the corpus past roughly 50 MB, this moves to fetch-on-demand.

Documents are registered by dropping PDFs into `corpus/` and recording them in the manifest, so the pipeline never depends on a scraper. A fetcher arrives in phase 7 for documents not yet supplied; it will be rate-limited and send an identifying user-agent, since there is no reason to hammer the ISO sites.

## License

MIT — see [LICENSE](LICENSE).
