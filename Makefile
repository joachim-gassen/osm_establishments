PYTHON ?= python

OSM_PLACES = data/pulled/business_places.geojson
WEB_EVIDENCE = data/pulled/legal_web_pages.json
LEGAL_INFO = data/generated/osm_establishments_legal_info.csv
MAP_PNG = output/berlin_establishments_url_map.png
BACKUP_ROOT = backups

OSM_PULL_SCRIPT = code/pull_osm_data.py
WEB_SCRAPE_SCRIPT = code/scrape_web_for_legal_info.py
LLM_PARSE_SCRIPT = code/parse_legal_entities_llm.py
LLM_INSTRUCTIONS = code/llm_instructions.md
MAP_SCRIPT = code/render_establishments_map.py

.PHONY: all map backup clean dist-clean

all: $(LEGAL_INFO)

map: $(MAP_PNG)

backup:
	@stamp=$$(date +%Y%m%d_%H%M%S); \
	backup_dir="$(BACKUP_ROOT)/pipeline_outputs_$$stamp"; \
	mkdir -p "$$backup_dir"; \
	cp -a data "$$backup_dir/data"; \
	cp -a output "$$backup_dir/output"; \
	echo "Backed up data and output to $$backup_dir"

clean:
	rm -f $(MAP_PNG)

dist-clean: clean
	rm -f $(OSM_PLACES)
	rm -f $(WEB_EVIDENCE)
	rm -f $(LEGAL_INFO)
	rm -rf data/cache

$(OSM_PLACES): $(OSM_PULL_SCRIPT)
	$(PYTHON) $(OSM_PULL_SCRIPT)

$(WEB_EVIDENCE): $(OSM_PLACES) $(WEB_SCRAPE_SCRIPT)
	$(PYTHON) $(WEB_SCRAPE_SCRIPT) --input $(OSM_PLACES) --output-json $(WEB_EVIDENCE)

$(LEGAL_INFO): $(WEB_EVIDENCE) $(LLM_PARSE_SCRIPT) $(LLM_INSTRUCTIONS)
	$(PYTHON) $(LLM_PARSE_SCRIPT) --input $(WEB_EVIDENCE) --output-csv $(LEGAL_INFO) --instructions $(LLM_INSTRUCTIONS)

$(MAP_PNG): $(OSM_PLACES) $(MAP_SCRIPT)
	$(PYTHON) $(MAP_SCRIPT) --input $(OSM_PLACES) --output $(MAP_PNG)
