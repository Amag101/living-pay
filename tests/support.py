"""All of Living Pay under one name for the tests, so they can read or replace anything in it."""
import importlib

NAMES = ("config", "store", "invoices", "connectors", "llm", "automations", "negotiator", "rules", "checkout",
         "paypal_events", "owner", "tour", "database", "web")


class App:
    def __init__(self):
        object.__setattr__(self, "modules", [importlib.import_module("living_pay." + n) for n in NAMES])

    def __getattr__(self, name):
        for module in self.modules:
            if hasattr(module, name):
                return getattr(module, name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        """Modules import what they use from each other, so a replacement has to reach all of them."""
        for module in self.modules:
            if hasattr(module, name):
                setattr(module, name, value)


app = App()
