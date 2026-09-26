"""Scoring and analysis. The ONLY package allowed to read data/answers/.

Nothing that builds Jev state (ingest, chunking, roster, questions, ledger,
render, inference, jev_client, friend_stubs, webapp, runners) may import this
package or read data/answers/. tests/test_answer_isolation.py enforces it.
"""
