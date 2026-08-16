# baresip-python development targets. Everything runs inside the uv-managed venv.
#
#   native  - build the static libre/libbaresip libraries
#   ext     - compile the cffi extension in place (fast C iteration)
#   test    - run the unit test suite
#   check   - audit built artifacts' linkage (advisory here; CI enforces on release)

.PHONY: native ext test check

native:
	uv run python scripts/build_native.py --prefix .native

ext:
	@echo "error: extension build is not implemented yet" && exit 1

test:
	uv run pytest tests/unit

check:
	uv run python scripts/check_linkage.py
