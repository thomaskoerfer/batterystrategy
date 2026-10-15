# Evaluation script safety

Before running a forecast, optimizer, shadow or perfect-foresight evaluator,
read the **Execution safety** section in `docs/evaluation/README.md`.

Production only collects evaluation inputs. Copy an immutable trace and feature
store snapshot to the local workstation, then run the evaluator locally with
explicit CPU and memory limits. Never execute a historical evaluator on the
Home Assistant host or inside a production container.
