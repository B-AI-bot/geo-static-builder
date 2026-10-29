.PHONY: build test serve

build:
	python3 geo_build.py example -o dist

test:
	python3 -m unittest discover -s tests -v

serve: build
	python3 -m http.server 8000 --directory dist
