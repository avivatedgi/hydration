import copy
import inspect
from collections import OrderedDict
from contextlib import suppress

from .fields import Field


class StructMeta(type):
    def __new__(mcs, name, bases, attributes):

        # Load all the fields from the parent classes
        base_fields = OrderedDict()
        footer_fields = OrderedDict()
        for base in filter(lambda x: issubclass(x, Struct), bases):
            for field_name in base._field_names:
                try:
                    if base._footer:
                        footer_fields[field_name] = getattr(base, field_name)
                except AttributeError:
                    base_fields[field_name] = getattr(base, field_name)

        # Check to see if any of the current attributes have already been defined
        for k, v in attributes.items():
            if k in base_fields or k in footer_fields:
                raise NameError("Field '{}' was defined more than once".format(k))

        # Update the current attributes with the fields from the parents (in order)
        base_fields.update(attributes)
        base_fields.update(footer_fields)
        attributes = base_fields

        # Create the list of the final fields and set the new attributes accordingly
        attributes['_bases'] = list(base.__qualname__ for base in bases)
        field_list = []
        for k, v in attributes.items():
            with suppress(AttributeError):
                if 'Struct' in v._bases:
                    # This field is a nested struct
                    field_list.append(k)
                    attributes[k] = v
                    continue
            if isinstance(v, Field):
                field_list.append(k)
                attributes[k] = v

        # Also save as attribute so it can be used to iterate over the fields in order
        attributes['_field_names'] = field_list

        return super().__new__(mcs, name, bases, attributes)

    @classmethod
    def __prepare__(mcs, name, bases):
        # Attributes need to be iterated in order of definition
        return OrderedDict()


class Struct(metaclass=StructMeta):
    @property
    def value(self):
        return self

    @value.setter
    def value(self, obj):
        vars(self).update(vars(obj))

    @classmethod
    def validate(cls, value):
        return type(value) == cls

    @property
    def _fields(self):
        # Use standard getattribute because custom returns the field values instead
        return (object.__getattribute__(self, name) for name in self._field_names)

    # noinspection PyArgumentList
    def __init__(self, *args, **kwargs):
        super().__init__()

        # Create a list of all the positional (required) arguments
        positional_args = []
        # if self is a subclass of Struct (and not Struct)
        if type(self) is not Struct:
            for name, param in inspect.signature(self.__init__).parameters.items():
                if param.default == inspect.Parameter.empty and param.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD:
                    positional_args.append(name)

        if len(args) != len(positional_args):
            raise ValueError('Invalid arguments were passed to the superclass. '
                             'Expected arguments: {} but {} given.'
                             .format(positional_args, len(args)))

        # from_bytes needs the args and kwargs to create the class
        # Set from_bytes as a private function
        self._from_bytes = self.from_bytes

        # Encapsulate _from_bytes so the arguments are automatically passed without changing from_bytes API
        self.from_bytes = lambda data: self._from_bytes(data, *args)

        # Deepcopy the fields so different instances of Struct have unique fields
        for name, field in zip(self._field_names, self._fields):
            setattr(self, name, copy.deepcopy(field))

    def __str__(self):
        x = [self.__class__.__qualname__]
        for name, field in zip(self._field_names, self._fields):
            if isinstance(field, Struct):
                x.append('\t{} ({}):'.format(name, field.__class__.__qualname__))
                x.extend('\t{}'.format(field_str) for field_str in str(field).splitlines()[1:])
            else:
                x.append('\t{}:\t{}'.format(name, field))
        return '\n'.join(x)

    def __len__(self) -> int:
        return sum(map(len, self._fields))

    def __eq__(self, other) -> bool:
        # noinspection PyProtectedMember
        # return all(a == b for a, b in zip(self._fields, other._fields))
        for a, b in zip(self._fields, other._fields):
            if a != b:
                return False
        return True

    def __ne__(self, other) -> bool:
        return not self == other

    def __bytes__(self) -> bytes:
        return b''.join(map(bytes, self._fields))

    @classmethod
    def from_bytes(cls, data: bytes, *args):
        obj = cls(*args)

        for field in obj._fields:
            split_index = len(field)
            field_data, data = data[:split_index], data[split_index:]
            field.value = field.from_bytes(field_data).value

        return obj

    def __getattribute__(self, item):
        """
        :param item:    The name of the item to get
        :return:        item's value, unless it's a field. In which case, the field's value
        """
        fields = super().__getattribute__('_field_names')
        if item in fields:
            return super().__getattribute__(item).value
        return super().__getattribute__(item)

    def __setattr__(self, key, value):
        """
        Has standard behavior, except when key references a field. In which case, set the field's value.

        :param key:     The name of the attribute to set
        :param value:   The value to set
        :return:        None
        """
        if key in self._field_names and not isinstance(value, (Field, StructMeta)):
            super().__getattribute__(key).value = value
        else:
            super().__setattr__(key, value)
