import copy
import inspect
import struct
from collections import OrderedDict
from contextlib import suppress
from pyhooks import Hook, precall_register, postcall_register
from typing import Callable, List, Iterable, Optional

from .helpers import as_obj, as_type
from .scalars import Scalar
from .fields import Field, VLA, TypeDependentLengthField
from .endianness import Endianness


class StructMeta(type):
    # noinspection PyProtectedMember
    def __new__(mcs, name, bases, attributes, endianness: Optional[Endianness] = None, footer: Optional[bool] = False):

        # Load all the fields from the parent classes
        base_fields = OrderedDict()  # Contains all the 'regular' fields
        footer_fields = OrderedDict()  # Similar to the 'regular' fields, but they will be appended in the end
        for base in filter(lambda x: issubclass(x, Struct), bases):
            for field_name in base._field_names:
                if hasattr(base, '_footer') and base._footer:
                    footer_fields[field_name] = getattr(base, field_name)
                else:
                    base_fields[field_name] = getattr(base, field_name)

        # Check to see if any of the current attributes have already been defined
        for field_name in attributes.keys():
            if field_name in base_fields or field_name in footer_fields:
                raise NameError("Field '{}' was defined more than once".format(field_name))

        # Update the current attributes with the fields from the parents (in order)
        base_fields.update(attributes)
        base_fields.update(footer_fields)
        attributes = base_fields

        # Save some metadata in private members
        # List of the parent classes of the class
        attributes['_bases'] = list(base.__qualname__ for base in bases)
        # Whether the Struct is a footer or not, used to determine order of attributes in child classes
        attributes['_footer'] = footer

        # Create the list of the final fields and set them as attributes
        field_names = []
        for field_name, field_obj in attributes.items():
            with suppress(AttributeError):
                # Check if the field_obj inherits from Struct, and is NOT actually `Struct` class
                if 'Struct' in field_obj._bases:
                    # This field_obj will be treated as a nested struct
                    field_names.append(field_name)
                    attributes[field_name] = field_obj
                    continue

            # Check if the field_obj is a Field, can't handle much else...
            if issubclass(as_type(field_obj), Field):
                field_names.append(field_name)
                obj = as_obj(field_obj)
                attributes[field_name] = obj

                # VLA fields require care - the Struct must resolve the VLA's length field name (if not already given)
                if isinstance(obj, VLA) and not obj.length_field_name:
                    # Look for the name of the field which has the VLA's length
                    for attr_name, attr in attributes.items():
                        if attr is obj.length_field_obj:
                            obj.length_field_name = attr_name
                            break
                    else:
                        raise RuntimeError('Unable to find field {} for VLA {}'.format(field_obj.length_field_obj,
                                                                                       field_obj))
                # If endianness was given, change endianness (only if it's default)
                if isinstance(obj, Scalar) and endianness:
                    # Check if the endianness_format was not already set
                    if not obj.endianness_format:
                        obj.endianness_format = endianness

        # Save field names as an attribute, used to iterate over the fields (in order)
        attributes['_field_names'] = field_names
        return super().__new__(mcs, name, bases, attributes)

    def __len__(self):
        return len(self())

    @classmethod
    def __prepare__(mcs, name, bases, *args, **kwargs):
        # Attributes need to be iterated in order of definition
        return OrderedDict()


class Struct(metaclass=StructMeta):
    __frozen = False
    _field_names: List[str]

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
    def _fields(self) -> Iterable[Field]:
        return (getattr(self, name) for name in self._field_names)

    # noinspection PyArgumentList
    def __init__(self, *args, **kwargs):
        # Create a list of all the positional (required) arguments
        positional_args = []
        # if self is a subclass of Struct (and not Struct)
        if type(self) is not Struct:
            for name, param in inspect.signature(self.__init__).parameters.items():
                # Check if the param is positional (required)
                if param.default == inspect.Parameter.empty and param.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD:
                    positional_args.append(name)

        if len(args) != len(positional_args):
            raise ValueError('Invalid arguments were passed to the superclass. '
                             'Expected arguments: {} but {} given.'
                             .format(positional_args, len(args)))

        # from_bytes and from_stream need the args to create the class, but shouldn't be required by default API
        # Set the functions as a private function
        self._from_bytes = self.from_bytes
        self._from_stream = self.from_stream

        # Encapsulate the functions so the arguments are automatically passed without changing from_bytes API
        self.from_bytes = lambda data: self._from_bytes(data, *args)
        self.from_stream = lambda data: self._from_stream(data, *args)

        # Deepcopy the fields so different instances of Struct have unique fields
        for name, field in self:
            # Validate the values
            with suppress(AttributeError):
                field.validator.validate(field.value)
            setattr(self, name, copy.deepcopy(field))
            # Initialize VLA length fields with proper values
            if isinstance(field, VLA):
                setattr(self, field.length_field_name, len(field))

        for k, v in kwargs.items():
            if k not in self._field_names:
                raise ValueError('Unexpected keyword argument given: {}={}'.format(k, v))
            setattr(self, k, v)

        super().__init__()
        self.__frozen = True

    def __str__(self):
        x = [self.__class__.__qualname__]
        for name, field in self:
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
        return all(a == b for a, b in zip(self._fields, other._fields))

    def __ne__(self, other) -> bool:
        return not self == other

    def __truediv__(self, other):
        from .message import Message
        return Message(self, other)

    @Hook
    def __bytes__(self) -> bytes:
        return self.serialize()

    def serialize(self) -> bytes:
        """
        Serialize the Struct object into bytes.
        You may use this function instead of bytes() if you don't want the bytes hook
        be hooked.
        """
        try:
            return b''.join(map(bytes, self._fields))
        except struct.error as e:
            raise ValueError(str(e))

    pre_bytes_hook = precall_register('__bytes__')
    post_bytes_hook = postcall_register('__bytes__')

    @classmethod
    def from_bytes(cls, data: bytes, *args):
        """
        Deserialize raw data from bytes into a Struct.

        :param data: The raw data to parse
        :param args: Arguments for the __init__ of the Struct, if there's any
        :return The deserialized struct
        """

        obj = cls(*args)

        for field in obj._fields:
            if isinstance(field, VLA):
                field.length = int(getattr(obj, field.length_field_name))
                field.from_bytes(data)
                data = data[len(bytes(field)):]
            else:
                if isinstance(field, TypeDependentLengthField):
                    split_index = len(field) * len(field.type)
                else:
                    split_index = len(field)

                field_data, data = data[:split_index], data[split_index:]
                field.value = field.from_bytes(field_data).value
                with suppress(AttributeError):
                    field.validator.validate(field.value)

        return obj

    @classmethod
    def from_stream(cls, read_func: Callable[[int], bytes], *args):
        """
        Deserialize a Struct object from a stream.

        :param read_func: The stream's reader function
        The function needs to receive an int as a positional parameter and return a bytes object.

        :param args: Arguments for the __init__ of the fields.
        :return: The deserialized object.
        """

        obj = cls(*args)

        for field in obj._fields:
            if isinstance(field, VLA):
                field.length = int(getattr(obj, field.length_field_name))
                data = read_func(field.length)
                field.from_bytes(data)
            else:
                if isinstance(field, TypeDependentLengthField):
                    read_size = len(field) * len(field.type)
                else:
                    read_size = len(field)

                data = read_func(read_size)
                field.value = field.from_bytes(data).value
                with suppress(AttributeError):
                    field.validator.validate(field.value)

        return obj

    def __iter__(self):
        """
        :return: Iterator of (name, field) tuples
        """
        return zip(self._field_names, self._fields)

    def __setattr__(self, key, value):
        """
        Only allows setting the field's values. Also updates length sources of VLAs and updates MetaFields

        :param key:     The name of the attribute to set
        :param value:   The value to set
        :return:        None
        """
        if key in self._field_names and not isinstance(value, (Field, StructMeta)):
            field = getattr(self, key)
            with suppress(AttributeError):
                field.validator.validate(value)
            field.value = value
            # Check if the field is a VLA
            if isinstance(field, VLA):
                # Set VLA source to the new length
                setattr(self, field.length_field_name, len(field))
        elif hasattr(self, key) or not self.__frozen:
            super().__setattr__(key, value)
        else:
            raise AttributeError("Struct doesn't allow defining new attributes")
