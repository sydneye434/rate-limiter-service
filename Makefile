# Rate limiter service – format/lint targets. Developed by Sydney Edwards.
format:
	python3 -m black app tests

lint:
	python3 -m black --check app tests

