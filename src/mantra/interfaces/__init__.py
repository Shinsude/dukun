"""Abstract contracts for every pluggable harness component.

The core agent loop depends only on these interfaces, never on concrete
implementations. Swap any component by registering a different class in
the registry and naming it in the configuration file.
"""
