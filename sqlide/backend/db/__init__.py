"""Database adapters. One folder per database, each exposing a Connector
implementation in its connector.py. The shared interface lives in base.py;
registry.py maps a connection kind to its adapter.
"""
