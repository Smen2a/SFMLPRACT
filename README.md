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

**In:** ISO-NE Market Rule 1 and appendices, ISO-NE manuals (M-11, M-20, M-28), NYISO MST/OATT and manuals (11, 12, ICAP 4), at a single point-in-time snapshot.

**Out, on purpose:** multi-version corpora and time-travel queries, section-level revision diffing, FERC order tracking, per-section effective-date indexing. In their place, queries carrying temporal cues ("in 2019", "used to", "prior to") are detected and abstained on, naming the snapshot actually held.

## Phases

- [x] **0a** Project scaffold, data model, lint/type/test tooling, CI
- [ ] **0b** Parser spike on three representative PDFs — *de-risks the highest-risk component before committing to a library*
- [ ] **0c** Link resolver, fetcher, and content-hashed manifest
- [ ] **1** Extraction → canonical text + page map → section tree → parse report
- [ ] **2** Chunker, FTS5, embeddings, sqlite-vec, weighted RRF
- [ ] **3** First ~30 gold questions and the Layer-1 retrieval eval
- [ ] **4** `search_result` assembly, the answer call, grounding verification
- [ ] **5** Full eval harness; gold set to ~120; judge validation
- [ ] **6** Glossary and cross-reference expansion, reranking — each with its ablation row
- [ ] **7** NYISO corpus and cross-ISO comparison queries
- [ ] **8** FastAPI + web UI with clickable citation highlighting

Evaluation (phase 3) deliberately precedes answering (phase 4), so retrieval is tuned against evidence rather than vibes. The UI is last: a UI built before the eval is how these projects end up looking impressive and being wrong.

## Development

```bash
uv venv && uv pip install -e ".[dev]"

ruff check . && ruff format --check .
mypy
pytest
```

Local embedding and reranking models live in the `local-models` extra (`uv pip install -e ".[dev,local-models]"`), since they pull torch and sit behind the `Embedder` / `Reranker` protocols.

### Corpus

`corpus/manifest.yaml` is committed; PDFs and the built index are not — `tariffrag ingest` rebuilds them reproducibly, and a 300 MB binary in git helps nobody.

The fetcher is rate-limited and sends an identifying user-agent. These are public regulatory filings, but there is no reason to hammer the sites. Documents can also be registered manually by dropping PDFs into `data/pdfs/`, so the pipeline is never hard-blocked on scraping.

## License

MIT — see [LICENSE](LICENSE).
