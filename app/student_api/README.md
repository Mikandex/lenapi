# Student API example

This example application is kept separate from the installable `lenapi` library.
It demonstrates relational models, migrations, Swagger documentation, and sync
and async CRUD routes.

Install lenApi and the development server from the repository root, then use
this application directory:

```bash
python -m pip install -e "../..[server]"
python lenora.py migrate
python lenora.py runserver
python lenora.py test
```

The app listens on `http://127.0.0.1:8009` and serves Swagger at `/docs/`.
Its SQLite database and uploaded media are local runtime data and should not be
included in source releases. The `tests/` directory is a Python package for
project tests; `python lenora.py test` generates endpoint checks temporarily,
runs them, and writes `outputs/lenapi_test_results.csv`.
