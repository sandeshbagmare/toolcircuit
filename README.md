# toolcircuit

A per-tool circuit breaker, so a dead API stops eating an agent's budget and context.

[![PyPI](https://img.shields.io/pypi/v/toolcircuit.svg)](https://pypi.org/project/toolcircuit/)
[![Python](https://img.shields.io/pypi/pyversions/toolcircuit.svg)](https://pypi.org/project/toolcircuit/)
[![tests](https://github.com/sandeshbagmare/toolcircuit/actions/workflows/ci.yml/badge.svg)](https://github.com/sandeshbagmare/toolcircuit/actions/workflows/ci.yml)
[![license](https://img.shields.io/pypi/l/toolcircuit.svg)](https://github.com/sandeshbagmare/toolcircuit/blob/main/LICENSE)

```python
>>> from toolcircuit import ToolCircuit
>>> circuit = ToolCircuit()
>>> circuit.state("search")
'closed'
>>> for _ in range(3):
...     state = circuit.record_failure("search")
>>> state
'open'
>>> circuit.allow("search")
False
>>> circuit.retry_after("search")
30.0
```

Retry understands a blip. It does not understand an outage: when a tool is down, forty
retries spend the run's tokens on forty copies of the same failure, and the agent never
reaches the sentence where it tells the user the service is unavailable. A breaker turns
"keep trying" into "stop, and say when to come back".

## Install

```console
pip install toolcircuit
```

## Use

```python
from toolcircuit import CircuitOpen, Policy, ToolCircuit

circuit = ToolCircuit(
    default=Policy(failure_threshold=3, cooldown=30),
    policies={"search": Policy(failure_threshold=2, cooldown=120)},
)

def call_tool(name, tool, **arguments):
    try:
        return circuit.call(name, tool, **arguments)
    except CircuitOpen as error:
        return {"error": str(error), "retry_after": error.retry_after}
    except Exception as error:
        return {"error": f"{name} failed: {error}"}
```

Reading it back:

```python
circuit.state("search")        # 'closed' | 'open' | 'half-open'
circuit.allow("search")        # False while open
circuit.failures("search")     # consecutive failures
circuit.retry_after("search")  # seconds until the next trial
circuit.report()               # {"search": {"state": "open", "failures": 3, …}}
circuit.reset()                # new run
```

## How the states work

| state | meaning | calls allowed |
| ----- | ------- | ------------- |
| `closed` | healthy, failures below the threshold | yes |
| `open` | threshold reached | no, `CircuitOpen` with a wait time |
| `half-open` | cooldown has passed | one trial |

A success anywhere closes the circuit and resets the failure count. A failure in
`half-open` reopens it for another full cooldown — the service had its chance.

The clock is injectable, which is how the tests avoid sleeping:

```python
now = 0.0
circuit = ToolCircuit(clock=lambda: now)
```

## Rules

- The breaker is per tool name, per process. A shared breaker across workers needs a
  shared store, which is a different library on purpose: a breaker that needs a network
  round trip is a breaker that breaks the run when that network is down.
- `call` re-raises the original exception after counting it, so your own error handling
  keeps working; only the refusal case is a new exception type.
- Count only failures that say something about the *service*. A bad argument is not an
  outage, so catch that before calling `call`, not after.
- `cooldown` is a floor, not a schedule: nothing wakes up to close the circuit, it closes
  on the first success after a cooldown, or on `reset()`.

## Limits

- No exponential growth of the cooldown between reopens, and no jitter. If you need
  either, wrap `Policy` and replace the breaker object.
- No bulkheads or concurrency limits: this decides *whether* to call, not *how many* at
  once.
- State dies with the process. That is the right default for a run, and the wrong one for
  a long-lived service — persist `report()` if you need that.

## Development

```console
python -m venv .venv
.venv/bin/pip install -e ".[test]"
.venv/bin/pytest
```

## License

MIT.
