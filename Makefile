.PHONY: test demo serve data mutate eval
PY ?= python3

test:            ## run the whole test suite
	$(PY) -m unittest discover -s tests

demo:            ## run every scenario and write reports/
	$(PY) -m glcc demo --out reports

serve:           ## run the API locally (offline mode, demo identity enabled)
	GLCC_LLM_PROVIDER=none GLCC_ALLOW_BODY_IDENTITY=true $(PY) -m glcc serve

eval:            ## retrieval evaluation
	$(PY) -m glcc retrieval-eval --out reports/retrieval_eval.md

data:            ## regenerate the sample PDFs and cases (needs requirements-dev.txt)
	$(PY) scripts/make_sample_data.py

mutate:          ## break the code on purpose; every breakage must be caught by the tests
	$(PY) scripts/mutation_check.py
