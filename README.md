# Glenstar-Backend
Flask API for the Glenstar Intelligence dashboard (deployed on Render via `render.yaml`).

## Underwriting data

`underwriting_markets.json` is the single source of truth for the frontend's Underwriting page,
served at `GET /api/underwriting/dataset`. To update it each quarter, edit the file (including
its `quarter` field) and run the data checks:

```bash
python -m unittest discover tests
```
