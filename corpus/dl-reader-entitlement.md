---
title: DL-Reader Entitlement Reference
owner: identity-team
last_updated: "2026-02-19"
tags: [dl-reader, entitlement, identity-portal, permissions]
---

# DL-Reader Entitlement

The **DL-Reader** entitlement grants read-only access to the curated data-lake zone. It is
the standard entitlement requested for analytics work and is provisioned through the
Identity portal.

## What it grants

- Read access to curated datasets (no raw zone, no write).
- Query access via the analytics gateway.

## How to get it

Request DL-Reader in the Identity portal and have your manager approve (see the data-lake
access runbook). For production datasets, DL-Reader is necessary but not sufficient — a
security review and Data Governance approval are also required.
