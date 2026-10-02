"""Hybrid retrieval with independent backend failures and truthful empty results."""
from __future__ import annotations
import sqlite3
from dataclasses import dataclass, field
from ...core import config, output
from ..indexing.index import connect, vault_available, vault_root
from . import bm25, tokenizer, vector
from .bm25 import Hit
@dataclass
class Result:
    chunk_id:str
    relative:str
    heading:str
    content:str
    score:float
    bm25_rank:int|None=None
    vector_rank:int|None=None
    matched:tuple[str,...]=()
    @property
    def source(self):
        return 'both' if self.bm25_rank is not None and self.vector_rank is not None else 'bm25' if self.bm25_rank is not None else 'vector'
@dataclass
class SearchOutcome:
    query:str
    mode:str
    strategy:str
    results:list[Result]=field(default_factory=list)
    warnings:list[str]=field(default_factory=list)
    error:str=''
    bm25_count:int=0
    vector_count:int=0
    @property
    def ok(self): return not self.error
def _normalize(values):
    if not values:return []
    low,high=min(values),max(values)
    return [1.0]*len(values) if high-low<1e-12 else [(v-low)/(high-low) for v in values]
def _fuse_rrf(lexical: list[Hit],semantic: list[Hit]):
    k=int(config.get('rrf_k',60)); merged={}
    for kind,hits in [('bm25',lexical),('vector',semantic)]:
        for rank,hit in enumerate(hits,1):
            if hit.chunk_id not in merged: merged[hit.chunk_id]=Result(hit.chunk_id,hit.relative,hit.heading,hit.content,0.0,matched=hit.matched)
            result=merged[hit.chunk_id]; result.score+=1/(k+rank); setattr(result,kind+'_rank',rank)
    return merged
def _fuse_weighted(lexical,semantic):
    merged={}
    for kind,hits,weight in [('bm25',lexical,float(config.get('bm25_weight',0.4))),('vector',semantic,float(config.get('vector_weight',0.6)))]:
        for rank,(hit,score) in enumerate(zip(hits,_normalize([h.score for h in hits])),1):
            if hit.chunk_id not in merged: merged[hit.chunk_id]=Result(hit.chunk_id,hit.relative,hit.heading,hit.content,0.0,matched=hit.matched)
            result=merged[hit.chunk_id]; result.score+=weight*score; setattr(result,kind+'_rank',rank)
    return merged
def _dedupe(results,per_file=2):
    seen=set(); counts={}; out=[]
    for result in results:
        fingerprint=' '.join(tokenizer.tokenize(result.content))
        if fingerprint in seen or counts.get(result.relative,0)>=per_file:continue
        seen.add(fingerprint); counts[result.relative]=counts.get(result.relative,0)+1; out.append(result)
    return out
def _rerank(query,results):
    try:
        terms=set(tokenizer.tokenize(query)); phrase=' '.join(tokenizer.tokenize(query))
        for result in results:
            body=' '.join(tokenizer.tokenize(result.content)); bonus=0.0
            if phrase and phrase in body:bonus+=0.25
            if terms:
                bonus+=0.15*len(terms & set(body.split()))/len(terms)
                if terms & set(tokenizer.tokenize(result.heading)):bonus+=0.1
            if result.source=='both':bonus+=0.05
            result.score=round(result.score*(1+bonus),6)
        return sorted(results,key=lambda r:-r.score)
    except Exception:return results
def search(query: str,mode=None,limit=None,conn: sqlite3.Connection | None=None):
    mode=(mode or str(config.get('search_mode','hybrid'))).casefold(); strategy=str(config.get('hybrid_strategy','rrf'))
    outcome=SearchOutcome(query,mode,strategy)
    if not query.strip():outcome.error='empty query';return outcome
    if mode not in {'bm25','vector','hybrid'}:outcome.error='invalid search mode';return outcome
    if limit is not None and limit<=0:outcome.error='limit must be positive';return outcome
    if not vault_available():outcome.error=f'vault unavailable at {vault_root()}; a missing mount is an error, not an empty result';return outcome
    owned=conn is None
    try:
        conn=conn or connect(); lexical=[]; semantic=[]; healthy=0
        if mode in {'bm25','hybrid'}:
            try:
                lexical=bm25.search(conn,query,int(config.get('bm25_top_k',20))); healthy+=1
            except Exception as exc:
                outcome.warnings.append(
                    f'BM25 unavailable. Falling back to VECTOR. ({exc})' if mode == 'hybrid'
                    else f'BM25 unavailable. ({exc})')
        if mode in {'vector','hybrid'}:
            try:
                status=vector.status()
                if not status.available:raise RuntimeError(status.reason)
                semantic=vector.search(conn,query,int(config.get('vector_top_k',20))); healthy+=1
                if not semantic and not vector.indexed(conn):
                    outcome.warnings.append('no current vectors indexed; run /skmr:obsidian-memory index')
            except Exception as exc:
                if mode=='vector':outcome.error=f'vector search unavailable: {exc}';return outcome
                outcome.warnings.append(f'Vector search unavailable. Falling back to BM25. ({exc})')
        outcome.bm25_count=len(lexical);outcome.vector_count=len(semantic)
        if not healthy:outcome.error='all requested retrieval backends unavailable';return outcome
        if not lexical and not semantic:return outcome
        if lexical and semantic:merged=_fuse_weighted(lexical,semantic) if strategy=='weighted' else _fuse_rrf(lexical,semantic)
        else:
            merged={h.chunk_id:Result(h.chunk_id,h.relative,h.heading,h.content,h.score,
                bm25_rank=i if lexical else None,vector_rank=None if lexical else i,matched=h.matched)
                for i,h in enumerate(lexical or semantic,1)}
        candidates=sorted(merged.values(),key=lambda r:-r.score)
        # Rerank before per-file dedupe so the relevant section is not discarded first.
        candidates=_rerank(query,candidates)[:int(config.get('rerank_k',10))]
        outcome.results=_dedupe(candidates)[:int(limit if limit is not None else config.get('final_k',6))]
    except Exception as exc:outcome.error=f'index unavailable: {type(exc).__name__}: {exc}'
    finally:
        if owned and conn is not None:conn.close()
    return outcome
def report(outcome,show_body=True):
    for message in outcome.warnings:output.warning(message)
    if outcome.error:output.error(outcome.error);return
    if not outcome.results:output.info('No matching permanent knowledge found.');return
    output.info(f'{len(outcome.results)} result(s) for "{outcome.query}" [mode={outcome.mode} strategy={outcome.strategy} bm25={outcome.bm25_count} vector={outcome.vector_count}]')
    for i,result in enumerate(outcome.results,1):
        heading=f' › {result.heading}' if result.heading else ''
        print(f'\n{i}. {result.relative}{heading}  (score={result.score:.4f}, via {result.source})')
        if show_body:
            body=result.content.strip()
            if len(body)>700:body=body[:700].rstrip()+' …'
            for line in body.splitlines():print('   '+line)
