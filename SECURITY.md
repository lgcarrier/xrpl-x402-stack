# Security Policy

## Supported Versions

Security fixes are expected to land on the latest state of the `main` branch.
Older unmaintained snapshots should be considered unsupported unless stated
otherwise.

## Reporting A Vulnerability

Please do **not** report security vulnerabilities through public GitHub issues.

Instead, use one of these paths:

1. GitHub private vulnerability reporting for this repository, if enabled.
2. Contact the maintainer directly through GitHub: [@lgcarrier](https://github.com/lgcarrier)

When reporting, include:

- a clear description of the issue
- impact and attack preconditions
- reproduction steps or a proof of concept
- any suggested mitigation, if you have one

You can expect:

- acknowledgement when the report is received
- an attempt to validate and triage the issue
- a coordinated fix and disclosure approach when appropriate

## Scope Notes

This project is payment infrastructure. Reports involving the following areas
are especially important:

- replay protection bypass
- incorrect payment verification
- incorrect destination validation
- settlement status misreporting
- denial of service through malformed transaction input
- configuration mistakes that could cause unsafe production behavior

General operational, legal, treasury, or exchange-risk questions are out of
scope for this security policy unless they are tied to a concrete software
vulnerability in this repository.
