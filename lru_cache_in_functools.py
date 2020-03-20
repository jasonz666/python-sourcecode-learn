## lru_cache 装饰器源码 注释说明
## 来源 python 3.6.8 functools.py


## LRU的实现用 循环双向链表与字典 字典用来保存链表结点
## 链表结点内包含函数执行的返回值（缓存内容）
## 字典的键是函数调用时传入实参列表整体（这里实参列表指所有实参的集合）的哈希值
## 不同的实参传入形式 由于哈希不同会当作不同的调用 然后存入返回值到缓存


## 缓存状态信息存储 为命名元组
_CacheInfo = namedtuple("CacheInfo", ["hits", "misses", "maxsize", "currsize"])


## 为函数实参列表计算哈希值
## 此类继承自list 原因暂时未知 可能是其他地方需要用到
## _HashedSeq类相当于构造了一个可以进行哈希的列表
class _HashedSeq(list):
    """ This class guarantees that hash() will be called no more than once
        per element.  This is important because the lru_cache() will hash
        the key multiple times on a cache miss.

    """

    ## 限制类的属性仅hashvalue
    __slots__ = 'hashvalue'

    def __init__(self, tup, hash=hash):

        ## 拷贝元组给列表 这样拷贝对self是原地修改 而self=tup[:]的self是新的
        self[:] = tup
        self.hashvalue = hash(tup)

    ## _HashedSeq类的实例作为字典键时自动调用哈希魔术方法获得哈希值作为key
    def __hash__(self):
        return self.hashvalue


## 生成key
## 使用被lru_cache装饰的函数（下面简称被装饰函数）的实参列表生成一个唯一的哈希值
def _make_key(args, kwds, typed,
              kwd_mark=(object(),),
              fasttypes={int, str, frozenset, type(None)},
              tuple=tuple, type=type, len=len):
    """Make a cache key from optionally typed positional and keyword arguments

    The key is constructed in a way that is flat as possible rather than
    as a nested structure that would take more memory.

    If there is only a single argument and its data type is known to cache
    its hash value, then that argument is returned without a wrapper.  This
    saves space and improves lookup speed.

    """

    ## 将实参列表合并到一个元组里 如果实参为 (1, 2, a=2, b=3)
    ## 实参打包后形式为 (1,2),{'a':2, 'b':3}
    ## 结果像这样 (1, 2, object(), 'a', 2, 'b', 3)
    key = args
    if kwds:
        key += kwd_mark
        for item in kwds.items():
            key += item
    
    ## 区分实参类型的情况
    ## 结果像这样 (1, 2, object(), 'a', 2, 'b', 3, int, int, int, int)
    ## 显然与上面不区分实参类型的情况 两者的哈希值不一样
    if typed:
        key += tuple(type(v) for v in args)
        if kwds:
            key += tuple(type(v) for v in kwds.values())

    ## 单实参时 实参本身作为哈希值 这样不用返回_HashedSeq的实例 加快查询速度
    elif len(key) == 1 and type(key[0]) in fasttypes:
        return key[0]

    ## 被装饰函数传入空实参时也走这里
    return _HashedSeq(key)


## lru缓存装饰器函数
def lru_cache(maxsize=128, typed=False):
    """Least-recently-used cache decorator.

    If *maxsize* is set to None, the LRU features are disabled and the cache
    can grow without bound.

    If *typed* is True, arguments of different types will be cached separately.
    For example, f(3.0) and f(3) will be treated as distinct calls with
    distinct results.

    Arguments to the cached function must be hashable.

    View the cache statistics named tuple (hits, misses, maxsize, currsize)
    with f.cache_info().  Clear the cache and statistics with f.cache_clear().
    Access the underlying function with f.__wrapped__.

    See:  http://en.wikipedia.org/wiki/Cache_algorithms#Least_Recently_Used

    """

    # Users should only access the lru_cache through its public API:
    #       cache_info, cache_clear, and f.__wrapped__
    # The internals of the lru_cache are encapsulated for thread safety and
    # to allow the implementation to change (including a possible C version).

    # Early detection of an erroneous call to @lru_cache without any arguments
    # resulting in the inner function being passed to maxsize instead of an
    # integer or None.

    ## 参数类型检查
    if maxsize is not None and not isinstance(maxsize, int):
        raise TypeError('Expected maxsize to be an integer or None')

    ## 装饰函数
    def decorating_function(user_function):

        ## 形成闭包 _CacheInfo为全局变量
        wrapper = _lru_cache_wrapper(user_function, maxsize, typed, _CacheInfo)

        ## update_wrapper将被包装函数的一些属性恢复到包装函数上
        return update_wrapper(wrapper, user_function)

    return decorating_function


## lru缓存核心函数
def _lru_cache_wrapper(user_function, maxsize, typed, _CacheInfo):
    # Constants shared by all lru cache instances:
    sentinel = object()          # unique object used to signal cache misses
    make_key = _make_key         # build a key from the function arguments

    ## 链表节点元素 节点就是一个列表
    PREV, NEXT, KEY, RESULT = 0, 1, 2, 3   # names for the link fields

    ## 缓存用字典实现
    cache = {}
    hits = misses = 0

    ## 缓存满标记
    full = False

    ## 字典get方法
    cache_get = cache.get    # bound method to lookup a key or return None
    cache_len = cache.__len__  # get cache size without calling len()

    ## 锁 线程锁？
    lock = RLock()           # because linkedlist updates aren't threadsafe
    root = []                # root of the circular doubly linked list

    ## 循环双向链表 根节点一开始指向自己
    root[:] = [root, root, None, None]     # initialize by pointing to self

    ## 不缓存 函数调用后 只是简单的更新lru缓存的misses值
    if maxsize == 0:

        def wrapper(*args, **kwds):
            # No caching -- just a statistics update after a successful call
            nonlocal misses
            result = user_function(*args, **kwds)
            misses += 1
            return result

    ## 无限缓存
    elif maxsize is None:

        def wrapper(*args, **kwds):
            # Simple caching without ordering or size limit
            nonlocal hits, misses

            ## 被装饰函数的实参表生成哈希作为字典key
            key = make_key(args, kwds, typed)
            result = cache_get(key, sentinel)
            
            ## 已缓存的结果 递增命中值
            if result is not sentinel:
                hits += 1
                return result
            
            ## 未缓存的结果 递增misses值
            result = user_function(*args, **kwds)
            cache[key] = result
            misses += 1
            return result

    #### lru缓存核心算法 ####
    else:

        def wrapper(*args, **kwds):
            # Size limited caching that tracks accesses by recency
            nonlocal root, hits, misses, full
            key = make_key(args, kwds, typed)
            with lock:
                link = cache_get(key)
                if link is not None:
                    # Move the link to the front of the circular queue
                    link_prev, link_next, _key, result = link
                    link_prev[NEXT] = link_next
                    link_next[PREV] = link_prev
                    last = root[PREV]
                    last[NEXT] = root[PREV] = link
                    link[PREV] = last
                    link[NEXT] = root
                    hits += 1
                    return result
            result = user_function(*args, **kwds)
            with lock:
                if key in cache:
                    # Getting here means that this same key was added to the
                    # cache while the lock was released.  Since the link
                    # update is already done, we need only return the
                    # computed result and update the count of misses.
                    pass
                elif full:
                    # Use the old root to store the new key and result.
                    oldroot = root
                    oldroot[KEY] = key
                    oldroot[RESULT] = result
                    # Empty the oldest link and make it the new root.
                    # Keep a reference to the old key and old result to
                    # prevent their ref counts from going to zero during the
                    # update. That will prevent potentially arbitrary object
                    # clean-up code (i.e. __del__) from running while we're
                    # still adjusting the links.
                    root = oldroot[NEXT]
                    oldkey = root[KEY]
                    oldresult = root[RESULT]
                    root[KEY] = root[RESULT] = None
                    # Now update the cache dictionary.
                    del cache[oldkey]
                    # Save the potentially reentrant cache[key] assignment
                    # for last, after the root and links have been put in
                    # a consistent state.
                    cache[key] = oldroot
                else:
                    # Put result in a new link at the front of the queue.
                    last = root[PREV]
                    link = [last, root, key, result]
                    last[NEXT] = root[PREV] = cache[key] = link
                    # Use the cache_len bound method instead of the len() function
                    # which could potentially be wrapped in an lru_cache itself.
                    full = (cache_len() >= maxsize)
                misses += 1
            return result

    def cache_info():
        """Report cache statistics"""
        with lock:
            return _CacheInfo(hits, misses, maxsize, cache_len())

    def cache_clear():
        """Clear the cache and cache statistics"""
        nonlocal hits, misses, full
        with lock:
            ## 清空字典操作
            cache.clear()
            ## 根节点重置
            root[:] = [root, root, None, None]
            hits = misses = 0
            full = False

    ## 给包装函数附加缓存相关函数
    wrapper.cache_info = cache_info
    wrapper.cache_clear = cache_clear
    return wrapper
