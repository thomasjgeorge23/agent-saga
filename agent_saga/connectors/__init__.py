"""Reference connectors.

Each is a worked example of one compensation class, and each is deliberately
honest about what it cannot undo. Import the submodule you need -- importing
this package does not pull in stripe, psycopg, or httpx.

IMPORTANT: saga-recoveryd must import the same connector modules as the agent.
The @compensator registrations live in module scope; a daemon that has not
imported them will escalate every dangling saga to NEEDS_HUMAN with
"handler not registered".
"""

from ._secrets import (
    CredentialError,
    SecretLeak,
    assert_no_secrets,
    resolve_credential,
    set_credential_resolver,
)
from .github import GitHubConnector
from .cloud import CloudConnector
from .messaging import MessagingConnector

__all__ = [
    "CredentialError",
    "SecretLeak",
    "assert_no_secrets",
    "resolve_credential",
    "set_credential_resolver",
    "GitHubConnector",
    "CloudConnector",
    "MessagingConnector",
]

