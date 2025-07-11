import os
import sys



def top_level_func():
    x = 1


    def inner_func1():
        y = 2


        def inner_inner_func():
            pass


        return y


    def inner_func2():
        pass


    class InnerClass:
        def method1(self):
            pass


        def method2(self):
            pass


    return x



def another_top_level():
    pass



class TopLevelClass:
    def __init__(self):
        pass


    def method(self):
        pass


    class NestedClass:
        def nested_method(self):
            pass


        def another_nested_method(self):
            pass
