---
name: optimizer-structured-output
description: Produce schema-valid Trace2Prompt v2 optimizer proposals for deterministic downstream validation and compilation.
---

# Optimizer Structured Output

Return one JSON object and no surrounding prose or Markdown. The object must
match the supplied JSON Schema exactly.

Before returning:

1. Include every required field and no unknown fields.
2. Use only enum values declared by the schema.
3. Keep field values semantic: schema validity does not prove that an update is
   applicable, executable, or useful.
4. Return `{"proposals": []}` when evidence does not justify a concrete update.
5. When a previous response and validation error are supplied, correct that
   error without changing evidence-backed values unnecessarily.

The caller validates every response. Invalid responses are returned with the
exact validation error for at most three total attempts. Never wrap JSON in a
code fence.
