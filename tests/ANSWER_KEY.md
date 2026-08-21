# Answer key for the test harness

`tests/fixtures/payments_after.py` contains seven planted defects. Use this to
grade what the reviewer actually reported.

| # | Defect | Class | Should the reviewer catch it? |
| --- | --- | --- | --- |
| 1 | Hardcoded `STRIPE_SECRET` | security / critical | Yes, always. If this is missed, something is wrong. |
| 2 | SQL injection via string concatenation | security / critical | Yes, always. |
| 3 | `except Exception: pass` swallows a failed charge | error-handling / high | Yes, reliably. |
| 4 | Mutable default argument `footer_notes=[]` | correctness / medium | Usually. Classic Python trap, well represented in training data. |
| 5 | `total / len(orders)` divides by zero on an empty list | correctness / medium | Usually. |
| 6 | N+1 query inside `enrich_orders` | performance / medium | Sometimes. Needs the model to notice the loop encloses a query. |
| 7 | File handle never closed in `write_audit_log` | correctness / medium | Sometimes. |

## How to read your score

- **1, 2 and 3 all caught** - the reviewer is working. These are the classes it
  is genuinely reliable at, and they are also the ones that actually hurt in
  production.
- **Fewer than three caught** - check the model name in the config and confirm
  the API key is valid. Something is misconfigured, not merely weak.
- **Findings that are not in this table** - not automatically wrong. The
  fixture is not perfect code, so a real extra finding is a good sign. A
  finding about code that is not in the diff is a false positive, and if you
  see several, tighten `min_severity` to `medium`.
- **6 and 7 missed** - expected on `gpt-4o-mini`. If you need these, that is
  the argument for switching `model` to `gpt-4o`.

## Verifying the line numbers

Every finding's line number should point at the real defect. Check two:
the hardcoded secret and the SQL concatenation. If those line numbers are off
by more than a line or two, the diff annotation is broken and inline comments
will land in the wrong place.
