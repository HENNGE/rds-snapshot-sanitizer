from enum import StrEnum

from faker import Faker

fake = Faker()

faker_methods = list(
    {
        method
        for provider in fake.get_providers()
        for method in dir(provider)
        if not method.startswith("_") and callable(getattr(provider, method))
    }
)


FakerEnum = StrEnum("FakerEnum", faker_methods)
