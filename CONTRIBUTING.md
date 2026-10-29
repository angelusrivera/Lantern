# Working on Lantern

Thanks for taking a look. Lantern is a small home lab project, so I try to keep it easy to run and avoid adding dependencies unless they solve a real problem.

## Get set up

You’ll need Python 3.10 or newer. From the project folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Run the app with the example config:

```bash
python -m lantern --config config.example.json
```

Run the tests before sending a change:

```bash
python -m unittest discover -s tests -v
```

If you add a setting or change how something works, please update the README or the relevant file in `docs/`. Please don’t include your `.env`, `config.json`, database, access logs, or real client details in a change.
