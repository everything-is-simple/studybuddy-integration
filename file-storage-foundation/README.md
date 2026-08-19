# File Storage Foundation Integration

This is an integration-only composition of the independently smoke-passed Composer backend parser and SQLite local storage. It retains originals under `H:\studybuddy-test\runs\file-storage-foundation\originals`, persists sanitized synthetic extraction data, closes/reopens SQLite, checks integrity, and restores through the SQLite backup API.

Run with the parser venv:

```powershell
H:\studybuddy-composer\components\backend-file-parsers\.venv\Scripts\python.exe run_integration.py
```

The integration code is not imported by `H:\studybuddy`.
