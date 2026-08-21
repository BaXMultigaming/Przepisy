# Recipe repository

Repozytorium jest automatycznie zasilane danymi z oficjalnego API TheMealDB.

- `index.html` – przeglądarka przepisów
- `data/recipes/*.json` – każdy przepis w osobnym pliku
- `data/recipes_manifest.json` – indeks wszystkich przepisów
- `assets/images/` – lokalne zdjęcia
- `scripts/sync_recipes.py` – synchronizacja danych

Workflow można uruchomić ręcznie z zakładki **Actions** i uruchamia się również automatycznie raz dziennie.

Jeżeli używasz repo jako publicznej aplikacji/serwisu, sprawdź aktualne warunki TheMealDB i w razie potrzeby dodaj sekret repozytorium `THEMEALDB_API_KEY`.
